# domains/costing/ —— 成本域

## 职责

Deal Cost Sheet：一个机会或报价的完整成本构成，以及由它推导的利润与底价。

## 两条硬边界在这里交汇

**硬边界 2（Decimal 确定性计算）**：单位完整成本、最低可售价、目标售价、毛利、贡献利润、利润率、最大允许折扣、最大允许获客成本——全部由纯函数用 `Decimal` 算。模型的职责是：识别可能遗漏的成本项、解释计算结果、提醒低利润风险、生成客户可读说明、比较方案。**模型不产出任何最终数字。**

**硬边界 7（价格基准门禁）**：成本表里产品采购价的 basis 是 `INDICATIVE` 时，这张表**不能**支撑客户可见报价。抓来的网页价格在没和供应商谈具体规格数量之前，对报价几乎没用——真实价格取决于数量档、材质等级、定制要求和谈判。用参考价报价等于闭眼承诺，单子越大亏得越多。例外只有一条路：人工明确接受风险，记录谁、何时、为什么。

## 三个成本版本

```text
ESTIMATED   估算。用参考价和经验值，供内部判断方向
QUOTED      报价时锁定。供应商实报价 + 锁定汇率快照
ACTUAL      实际发生。事后核算用
```

**QUOTED 版本一经关联报价即不可变。** 供应商后来改价就生成新版本，历史版本保留——否则「我们上周报给客户的价格是怎么算出来的」这个问题无法回答，而客户回头砍价时你恰恰需要这个答案。

## 成本项必须完整

漏一项成本就是虚增一分利润。`CostItemType` 枚举覆盖设计稿第 18 节全部 22 项（采购、样品、模具、定制、印刷、包装、质检、损耗、国内运、国际运、保险、报关、关税、目的地运、仓储、支付手续费、佣金、获客、数据、广告、Agent/API、售后预留）。模型的「遗漏项提醒」就是对着这张清单查的。

## 汇率

每张成本表绑定 `FxSnapshot`。报价版本锁定当时的汇率快照，绝不用「当前汇率」重算历史。

## 依赖白名单

```text
允许   shared.events, shared.schemas, shared.errors
禁止   任何其他 domains/*、任何外部 SDK
```

## 发布 / 订阅

发布：无（成本表变化通过 quotations 域的事件间接可见）
订阅：`SourcingCaseHandedToCosting`（用已确认 primary Option 的候选价格起 ESTIMATED 版本）；旧 `SourcingCaseCompleted` 只作兼容读取。

## 禁止事项

- 不允许 float 出现在任何金额字段
- 不允许覆盖已关联报价的 QUOTED 版本
- 不允许 INDICATIVE 基准的成本表直接进报价（无人工风险接受记录时）
- 不允许模型写入金额——金额字段的 Provenance `extracted_by` 若是模型，该值只能作为「建议」进入待确认区，不能直接参与计算

## V2 sourcing handoff

仅在 Case 已有 boss 确认的 review 和 Opportunity 时消费 handoff。每个 Case 只产生一个
`ESTIMATED` CostSheet 与一个适用的 primary `product_purchase` 项，金额以 `Decimal` 保存、wire 为
字符串，price basis 始终 `indicative`；Opportunity 缺失必须保持 `opportunity_required`，不创建半成品成本表。

## Phase 2 计算约束

- `costing-v1` 计算必须在固定 50 位 Decimal 上下文执行；不得读取系统时钟、当前汇率、数据库或环境默认政策。
- 客户展示金额先舍入单价、再计算并舍入行额；利润使用最终行额除以数量后的有效核算收入重算。
- 报价币种换算只接受锁定的核算币种到报价币种直连汇率；不得自动查反向汇率或拼接汇率路径。
- `inputs_hash` 覆盖成本、依据、数量、币种、汇率、归类、政策、报价实际价格、精度和算法版本；Decimal 规范化，列表重复项不得去重，显式计算时间不参与哈希。
- 全成本利润率不得标为毛利率；可折扣空间和可追加获客成本空间均不构成授权或预算。

## Phase 1 范围

成本项枚举、三版本模型、Decimal 计算接口、价格基准门禁。Phase 1 寻源报价由人工完成，但**人工填的成本也走这套结构**——否则 Phase 2 自动化时历史数据格式对不上。

## Phase 2 人工依据确认

- 政策、采购/费用依据、完整性清单和报价汇率只增留痕；新政策路径不把历史未确认 margin_rules 升级为授权。
- 确认只接受当前在职员工事实。政策确认仅 boss；原文读取后写入前再次核验身份，不能用请求角色或确认人自证权限。
- 原件读取必须由可信 PricingEvidenceReader 按 tenant 与 actor_id 校验 ACL、内容 hash 和原文定位；持有来源引用不代表读取授权，真实 IO 仍走 Gateway。
- 供应商采购与实际费用不能混淆。费用单件/整单口径及适用数量必须显式确认，不猜自由文本、不伪造产品 MOQ；正式采购只接受适用数量档内的 quoted。
- 完整性清单逐一覆盖22类并绑定持久 item_sequence；缺项不是零。清单不得隐藏已确认成本，不得重复同一原文费用明细和分摊范围，不得混用获客汇总/明细。
- 来源字段必须完整 Provenance，WEB_PAGE 必须保留真实 URL 与 hash；可信来源读取和人工确认不等于模型证明了供应商价款真实性。
- 清单存在不代表可以报价；后续冻结必须重新验证当前 Need 上下文、政策、证据适用性与有效期。

## Phase 2 人工适用性与创建操作

- scope确认显式绑定完整Need事实、条款、期限与全部持久依据ID/hash及逐项人工适用性说明；材质、包装或任何来源变化均需重新确认，不自动复用旧scope。
- 原供应商自由规格必须保留，不把它与确定性客户规格JSON做文本等价判断。单位、目的地、数量档、MOQ、quoted基准及有效期仍是不可绕过的硬检查。
- scope和T2共用同一22项费用覆盖校验，不能以人工说明替代金额、币种、单件/整单口径及重复分摊检查。
- 原件授权在context与成本锁外完成；仅可信内部application可传递本次准确绑定的来源授权投影，不向HTTP开放授权票据或敏感facts快照。
- scope、basis及创建意图是只增历史；0043不证明真实报价存在，完成回执必须由后续可信报价持久reader提供。
- 冻结固定creation key→sheet→政策集合共享锁顺序，取得锁后才取本轮时钟；政策确认以相同tenant集合独占锁保护，原件读取在锁外。
- 同key绑定全部创建意图；pending不能换key绕过。未知提交只按原key查操作，不自动解锁或换键。完成后仅显式旧quote/version回执匹配才可复用未变锁表，并需新scope/basis。
- basis完整保存`cost_fx_rates`表内核算汇率元组，与独立`quote_fx`及实际PricingOptions区分；修订可选新已确认报价FX，不能改旧成本FX/locked_at。

## Phase 2 审批政策选择租约

`CostingApprovalPolicyReader`只向可信报价审批编排提供当前政策selection，不扩展get_policy角色或HTTP。
它复用本域政策选择算法及tenant政策集合shared advisory锁，`current()`每次取新的注入时钟，按当前
业务category重新选择，不能只锁历史政策行或用历史global.category掩盖新specific政策。
报价机会锁后才取得政策租约；报价commit/rollback完成后才关闭。政策确认仍用同集合独占锁。
缺政策/不同id或hash由报价固定policy_stale阻断，不能补默认政策或重算已确认的旧报价数字。

## Phase 2 安全资料刷新

新增安全读取仍先核当前C；价格集合用同UoW的opportunity_refs.exists区分真实空集合
和缺对象/跨租户，内部bool事实口不授予CRM权限、不投影客户字段。SQL故障不变False。
新增安全缺对象错误为CostingQuoteNotFoundError。T2 get_policy/get_quote_fx仅真实无记录
分支使用此ValidationError子类；当前有效政策选择/默认fallback/确认逻辑不变。
旧get_sheet缺表用CostSheetNotFoundError并保留“成本表不存在”文本及ValidationError兼容；
只有新报价HTTP映射404，旧成本HTTP保持400。add_item/readiness不改。费用确认合并的
缺表/变更CAS仍CostCoverageConflict，新HTTP为409 coverage_stale，不拆查询或误称幂等冲突。
coverage按精确原hash恢复或读取最新；scope按确认时间/ID发现只增历史，刷新不补确认。
public DTO逐值白名单投影，不含完整Need、来源原文或locator；安全摘要不代表原件读权。
