# ADR 0023：Sourcing Case V2 事件与契约

## 状态

已接受。本文只描述免费公开寻源→候选产品卡→人工审核→ESTIMATED 成本交接子项目；不宣称整个 Phase 2 完成。

## 决策

- `ValidatedNeed` 的寻源门槛统一是由事实字段确定性推导出的完整度 `3`。首次满足时发布 `NeedValidated`；在后续补全中跨过门槛时只发布一次 `NeedBecameSourcingReady`。两事件兼容消费，按 Need 的 canonical trigger key 收敛为一个 V2 Case/Run。
- V2 使用八步 Workflow：内部梯子、等待计划、公开搜索、候选核验、候选准备、等待产品卡、等待审核、成本交接。内部 rung 1–5 必须逐级留下 no-match 或 match 证据；未达到前不得进入公开搜索。
- 公开计划先草拟，再由 boss 以精确 plan hash 确认并运行。替换计划使旧确认失效。免费额度为 unknown、paid 或不足时 fail closed；不确定请求保留额度并要求人工 reconciliation，绝不自动重发。
- `SourcingCandidatesVerified` 是封存候选集到 `source_only` Product/Supply Option 的唯一投影触发；`SourcingCandidatesReady` 只表示完整产品卡集已经准备好。审核先由 sourcing 人员提交事实，再由 boss 对同一选择精确确认；成本交接只在 Opportunity 已存在时发生，否则停在 `opportunity_required`，补齐后用同一 Run 重试。
- 公开价格永远是 `INDICATIVE`。`quoted_prices` 没有新的公开寻源写入路径；只有受信供应商对明确规格/数量的报价证据才能产生 quoted，并且才可能进入客户可见 Quote。
- 公开页面 URL 的写入、候选证据读取与 Gateway 使用同一 canonical 安全形状：拒绝凭证、片段、控制字符、非默认端口、数值/特殊网络主机和不安全重定向；抓取边界仍额外执行 DNS/对端地址检查。重试只恢复同一持久回执/页面槽，不重新扩张预算。
- Case 打开时冻结 `need_snapshot`，Candidate 的 canonical required specs/value 只能从这份快照重建：`product_type ← product_category`、`material`、`size ← size_spec`、`application`，以及仅在 Need 声明时出现的 `model`。Need 已声明的 spec 不得被 Candidate 省略或漂移；Need 未声明 `model` 时不得凭空把它设为必填。Candidate 的公开观察词表使用 `product_type`、`size`；内部 Product 的规范词表使用 `product_category`、`size_spec`，不得互相推断或重命名。模型输出只形成 immutable calibration draft；完整但不合格的结果是带结构化理由的 rejected Candidate。草稿幂等性绑定不可变公开来源/Artifact，而非自由文本。
- `material` 与 `size` 也不能由 Candidate 在 Need 快照缺失时补造。完整度 `3` 但未声明其中任一字段的 Need 必须先补全/验证；提交含该 required 值的 Candidate 被拒绝，不能借调用方输入形成 canonical 比较事实或晋升 qualified。
- 本子项目以 `ValidatedNeed` 为前置状态。`DemandSignalCaptured` 与 `NeedHypothesisCreated` 在共享 catalog 中分别保留 demand/prospecting 的真实下游语义，Sourcing runtime 不得以无副作用 acknowledgement 抢先将其标记 delivered。仅 `SourcingCaseOpened` 与 `OpportunityQualified` 注册具名、tenant-bound 审计 acknowledgement；它们只确认已消费，不发送、创建联系人、采购或报价；全局“无 handler 即 dead”策略不改变，受控链要求该 tenant 的 dead outbox 为零。
- `opportunity_required` 的恢复必须跨真实 scheduler/runtime 重启仍只使用同一个 Case/Run，并只创建一个 `ESTIMATED` CostSheet。错误 tenant 的写命令在 API tenant guard 前失败，不能改变该 owner tenant 的 Case、Run、CostSheet 或 ToolCall。
- HTTP 只暴露允许的 Case、计划、候选、审核、reconciliation、Product 与 Run 安全投影。所有命令均按角色和路径 Case ID 授权；confirm/run/review/reconcile 还强制原始 `Idempotency-Key`。请求体不能携带 tenant、actor、Opportunity、CostSheet 或凭证字段。`lane` 当前没有持久化事实，因此为 unknown/null，不能从查询文本推断。前端只消费生成 OpenAPI 类型：旧 plan 确认失效、主选恰好一个/备选至多两个是 UX guard，后端仍是最终裁决。

## 后果

系统宁可停止并要求人工确认，也不因免费额度、页面、证据或机会关系不确定而发送、询价、采购或报价。联系人发现、邮箱验证、发信、真实 direct supplier quote 与商业数据源仍不是本 ADR 的能力。
