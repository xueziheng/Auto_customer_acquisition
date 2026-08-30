# Phase 2：自动 Sourcing Case 与候选产品卡设计

> 状态：已批准
> 日期：2026-08-30
> 范围：Phase 2 子项目；单租户运行方式；免费公开来源；Tavily basic；安全页面读取

## 一、背景与目标

Phase 2 已具备免费来源客户发现，以及成本、报价的工程闭环，但从
`Validated Need` 到供应候选之间仍是人工寻源骨架。现有仓库已经定义了
`SourcingCase`、七级匹配梯子、八项候选核验、三候选上限和
`source_only` 产品池，但尚无持久化实现、自动工作流、公开寻源编排和
人工选择后的成本交接。

本子项目完成以下能力：

1. 完整度达到门槛的 `Validated Need` 幂等创建 Sourcing Case；
2. 按固定顺序检查匹配梯子 1–5，优先复用内部产品与供应网络；
3. 前五级无合格供给时，生成有边界的公开寻源计划，经人工确认后复用
   Tavily basic 和现有安全页面读取器；
4. 从原始网页证据提取并确定性核验最多三个供应候选；
5. 为尚无产品记录的合格候选生成独立的 `source_only` 候选产品卡；
6. 人工选择一个主候选及最多两个备选后，才把主候选交给成本域创建
   `ESTIMATED` 成本表；
7. 让额度、证据、停止原因、人工决定和恢复状态完整可审计。

该能力仍属于同一套 TradeOS 的 Phase 2，不增加新 Phase。它服务于
“每消耗一单位成本产生的合格贸易机会数”这一北极星指标，不以搜索量、
供应商数量或产品卡数量为成功指标。

## 二、明确不做

- 不建设 1688、Alibaba、贸易数据库或其他商业数据商专属客户端；
- 不把预留 Protocol 描述成“填密钥即可使用”；
- 不修改 Tool Gateway 核心检查管线；
- 不自动采购、自动询价、自动议价、自动承诺交期或自动发送报价；
- 不把公开网页价格升级成 `QUOTED`；
- 不从供应商页面生成 `Validated Need` 或合格贸易机会；
- 不调用联系人补全、邮箱验证或发信能力；
- 不在本子项目实现 NeedCluster 排序、多提供商联系人瀑布、70/30 自适应
  分配或接管队列背压；
- 不改变 Phase 1 的真实运营验收状态；
- 不把受控测试冒充真实联网验收。

## 三、已批准的业务决定

### 3.1 自动开案与外部费用授权

当 `NeedValidated` 的确定性完整度达到 3 时，系统自动、幂等创建零外部
成本的 Sourcing Case。内部匹配梯子 1–5 可以自动执行。只有进入第 6 级
公开寻源前，才要求人工确认目标国家、品类、查询上限、页面上限和额度
预算。

需求首次升级为 `Validated Need` 时可能尚未达到完整度 3，之后补齐数量才
跨过门槛。V2 因此同时消费 `NeedValidated(completeness >= 3)` 与新增的
`NeedBecameSourcingReady` 事实；两条入口共用同一个开案幂等键。不得依赖
定时全表扫描弥补缺失事件，也不得让后续补齐的需求永久漏开 Case。

未确认时停在待授权状态，不调用 Tavily，也不预先消耗额度。

### 3.2 候选产品卡

每个尚无现有产品记录的合格供应候选自动生成一张独立的 `source_only`
产品卡。单个 Case 最多三个合格候选；相同 Case 与候选的重复事件不得
重复建卡。人工批准前，产品卡不能进入客户可见报价。

匹配梯子 1–3 命中已有产品时复用产品 ID，不复制一张新产品卡。梯子
4–6 命中尚无产品记录的合格候选时，才创建候选产品卡。

### 3.3 人工选择与成本交接

机器核验和产品卡生成完成后，Case 进入 `candidates_ready`。员工审核后
必须指定一个主候选，可以保留最多两个备选，并记录选择理由。只有主候选
进入成本核算；备选保留用于议价或主候选失败后的人工替换。

未经人工选择，Case 不得进入 `handed_to_costing`，成本域调用次数必须为
零。

成本表当前必须绑定 `OpportunityId`。人工提交选择后，V2 通过机会域公共
服务按同一 `need_id` 精确读取机会；不存在时保持 `candidates_ready` 并停止
为 `opportunity_required`，不得伪造机会或创建游离成本表。存在时把该
Opportunity 引用与人工选择一起固化，再交接成本域。

## 四、方案选择

### 4.1 采用：独立的 `sourcing_case_v2` 工作流

V2 复用现有 Tavily、免费额度账本、安全页面读取器和 Tool Gateway，新增
寻源专用的计划、状态、仓储、证据和人工选择契约。以后接入新的供应来源
时，通过连接器和工具插件点增加适配器，不修改寻源域不变量或 Gateway
核心管线。

### 4.2 未采用：复用客户发现工作流

客户发现与供应寻源的主体、证据含义、停止原因和预算授权不同。复用同一
工作流会把“企业可能是买家”与“企业可能能供货”混为一种候选，并使后续
状态与审计难以解释。

### 4.3 未采用：首批直接开发平台专属连接器

1688、Alibaba 等平台涉及登录、反爬、授权、页面稳定性和平台条款，会
扩大首批范围。当前先用公开供应商页面验证业务闭环；平台连接器以后仍须
单独实现、授权和测试。

## 五、总体架构与数据流

```text
NeedValidated（completeness >= 3）
或 NeedBecameSourcingReady
        │ 幂等消费
        ▼
自动创建 Sourcing Case（无外部费用）
        │
        ▼
按顺序检查匹配梯子 1–5
        │
        ├─ 找到内部合格供给 ───────────────┐
        │                                  │
        └─ 未找到                           │
              │                            │
              ▼                            │
        生成公开寻源计划                     │
              │ 人工确认                    │
              ▼                            │
        Tavily basic 搜索                   │
              │                            │
              ▼                            │
        安全页面读取与不可变快照              │
              │                            │
              ▼                            │
        候选提取与确定性核验                  │
              └─────────────┬──────────────┘
                            ▼
                  最多三个合格供给候选
                            │
                            ▼
              新候选生成 source_only 产品卡
                            │
                            ▼
                     candidates_ready
                            │ 人工选择
                            ▼
                   handed_to_costing
                            │
                            ▼
                主候选的 ESTIMATED 成本表
```

工作流是确定性协调者：读取需求快照、调用产品与供应商公共服务、协调外部
工具、推进状态和发布事件。sourcing 域不导入其他业务域；跨域协作只在
workflow 层通过各域 `service.py` 的显式接口完成。

Agent 只负责从已安全读取的页面中生成候选草稿和逐项匹配解释。它不决定
状态转换、完整度、额度、合格与否、候选上限、金额或客户可见性。

## 六、匹配梯子

必须严格按现有顺序执行，不允许直接跳到公开搜索：

```text
1  正式产品完全匹配
2  正式产品可修改后匹配
3  候选产品匹配
4  现有供应商有类似产品
5  供应商可定制
6  公开寻源
7  寻找全新工厂
```

每一级产生独立的 `LadderCheck`，记录输入快照、检查结论、匹配对象引用、
逐项规格比较、证据引用和时间。只有前五级均留下“无合格供给”的可解释
结论后，才能生成第 6 级计划。

在较早梯级找到合格供给时停止向后搜索。匹配结果不得保存相似度百分比，
必须逐项表达完全匹配、不同、未知、可否替代、替代影响和是否需要客户
确认。

## 七、数据契约

所有持久化表都带 `tenant_id`；所有唯一约束、索引、查询、更新和删除都
包含租户边界。

### 7.1 `SourcingNeedSnapshot`

开案使用强类型、不可变的需求快照，而不是只传 `need_id`：

- `need_id`、确定性 `completeness` 与推导规则版本；
- 品类、规格、数量、数量单位、目的地和时间要求；
- 每个关键字段的 Provenance；
- 快照观察时间和内容哈希。

`open_case` 接收强类型命令并在 sourcing 服务内验证完整度至少为 3。
这修复现有服务文档要求检查完整度、但方法参数无法实际执行检查的缺口。

### 7.2 `SourcingCase`

在现有模型上增加：

- `workflow_version`；
- `trigger_key`（tenant、Need ID 与工作流版本组成的稳定业务幂等键；不含入口
  事件类型，因此 `NeedValidated` 与 `NeedBecameSourcingReady` 共用同一键）；
- `need_snapshot_hash`；
- `active_search_plan_id`；
- `stop_code` 与安全的结构化详情；
- 乐观并发版本；
- 状态时间戳。

同一租户、Need 和工作流版本只能有一个活跃 Case。

### 7.3 `LadderCheck`

每条检查至少包含 Case、梯级、顺序号、结论、匹配对象类型与 ID、逐项
匹配解释、证据引用、检查者和时间。数据库与域服务共同拒绝跳级或覆盖
既有不可变检查事实。

### 7.4 `PublicSourcingPlan`

计划包含：

- 目标国家与品类；
- 查询文本和所属寻源线路；
- 查询数与页面数上限；
- 提供商及固定的 `basic` 能力要求；
- 账户用量快照与最坏情况额度预留；
- 计划版本、规范化内容哈希；
- `pending_confirmation`、`authorized`、`running`、`exhausted`、
  `blocked`、`completed` 等状态；
- 确认人、确认时间和授权时看到的计划哈希。

Case 可以保持 `discovering`，等待授权等细分状态由计划表达，从而不破坏
现有 Case 状态枚举。计划任何影响范围或成本的变化都创建新版本，并使旧
确认失效。

### 7.5 `SupplierCandidate`

候选将以下内容结构分离：

- `observed_facts`：页面明确出现且逐字段关联 EvidenceSnapshot 的事实；
- `supplier_claims`：供应商或目录自述；
- `match_inferences`：Agent 或员工对可替代性等做出的推断；
- `spec_comparisons`：产品类型、材质、尺寸、型号的逐项比较；
- `indicative_price_tiers`：数量档对应的 `Money/Decimal`；
- MOQ、计价单位和币种；
- 一个或多个网页快照引用；
- 核验状态、拒绝原因和核验者。

V2 不再新写语义错误的 `quoted_prices`。旧字段仅在旧 DTO/数据读取边界
兼容，迁移为明确的 `indicative_price_tiers`；它不能进入客户可见报价。

### 7.6 产品来源与人工审核

产品域用 `(tenant_id, case_id, candidate_id)` 唯一来源键幂等生成产品卡。
`ProductService.create_candidate_from_sourcing` 的自由 `dict` 改为强类型输入，
包含 Case、Candidate、证据、规格和 INDICATIVE 价格引用。

`SourcingReview` 保存唯一主候选、最多两个备选、审核人、时间、选择理由
和提交时的 Case 版本。主候选必须合格并已有现有产品引用或 `source_only`
卡；并发的过期审核提交通过条件更新拒绝。

现有产品只有同时具备内部成本金额、成本口径和可追溯来源时，才属于可自动
交给成本域的供给选项；资料缺一项时仍可作为梯子命中记录，但不得把未知成本
当成零或据此创建 `ESTIMATED` 成本表。

审核提交后通过机会域显式服务接口按 `need_id` 精确读取 `OpportunityId`。
只有真实存在且同租户的机会引用才写入 Case；不存在不是“无供应”，而是
独立的 `opportunity_required` 停止原因。

## 八、状态机与事件兼容

V2 使用现有 Case 状态：

```text
opened -> discovering -> verifying -> candidates_ready -> handed_to_costing
   │          │              │
   └──────────┴──────────────┴──> failed
```

状态、审计事实与 Outbox 事件在同一数据库事务提交。非法转换、缺少连续
阶梯检查、缺少合格候选或缺少人工选择时均拒绝转换。

现有骨架把“找到候选”“生成产品卡”“交给成本核算”都压在
`SourcingCaseCompleted` 上，无法表达已批准的人工门槛。V2 新增过去式事实
事件：

```text
SourcingCaseOpened
SourcingCandidatesVerified
SourcingCandidatesReady
SourcingCaseHandedToCosting
NeedBecameSourcingReady
```

- `SourcingCandidatesVerified` 只携带 Case 与精确的合格 Supplier Candidate
  IDs；产品域消费该事实并幂等生成 `source_only` 产品卡，再由 SYSTEM 调用
  sourcing 域窄接口登记真实 ProductId 与 Candidate 的供给选项；
- 产品卡和全部合格供给选项均已存在后，workflow 才以仓储重建出的完整集合
  调用最终就绪入口。sourcing 域转为 `candidates_ready` 并发布
  `SourcingCandidatesReady`；该事件表示完整冻结集合已就绪，不再作为建卡请求；
- `SourcingCaseHandedToCosting` 只携带 Case、Need、Opportunity 和 Review 的
  稳定引用；成本域读取同租户、同版本的 handoff snapshot，按主
  `SourcingSupplyOption` 创建唯一 `ESTIMATED` 成本表，不假设主选项必然来自
  Supplier Candidate；
- demand 域仅在需求状态首次跨到 `sourcing_ready` 时发布
  `NeedBecameSourcingReady`；重复补充字段或显式重复标记不重复发布；
- 旧人工流程继续按旧工作流版本解释 `SourcingCaseCompleted`；
- 产品与成本消费者在迁移期按事件/工作流版本路由，不把历史 Case 自动
  改成 V2，也不让一个 V2 Case 被两个事件重复处理。

上述事件拆分替代了旧稿中“产品域消费 `SourcingCandidatesReady` 并生成产品卡”的
冲突文本，决策与兼容后果见 ADR 0023。事件契约和依赖边界的变化必须留 ADR。

## 九、免费搜索、额度与恢复

公开寻源固定执行：

```text
计划已确认
  -> 读取 Tavily /usage 与账户付费状态
  -> 账户级事务锁定并预留最坏情况额度
  -> 调用 basic 搜索
  -> 持久化响应状态
  -> 经安全读取器读取原始网页
  -> 按确定结果核销或释放余额
```

规则：

- 付费已开启、计划未知、用量读取失败或余额不足时全部阻断；
- 不自动升级，不回退到付费来源；
- 多个 Run 按绑定 Tavily 账户共用持久化额度账本；
- 同一搜索执行使用稳定请求 ID；重启先读已有响应与预留，不重复调用；
- 外部结果不确定时保留预留，标记 `reconciliation_required`，不自动重试；
- 重新执行前必须人工核对提供商侧用量；
- 查询数、页面数和账户额度三重限制同时生效；
- credit 消耗规则来自连接器能力与 `/usage`，不硬编码成永久承诺；
- 搜索 credits、模型成本和基础设施成本分别记录。

## 十、证据、去重与安全

搜索摘要只用于定位 URL，不能成为候选事实。页面必须经过现有安全读取器
的私网地址、DNS、重定向、robots、登录墙、验证码和访问禁止检查。成功
读取后保存 URL、观察时间、内容哈希和 Artifact 引用。

网页中的任何指令均视为不可信数据。Agent 只获得脱离凭证的安全页面内容；
Tavily 密钥只存在 Connector 与 Gateway 的凭证解析/传输路径。密钥、完整
页面正文、公开邮箱和其他原始敏感内容不得进入日志或业务事件。

候选优先用已核验官网域名去重；平台页面使用稳定卖家 ID。只有普通目录
URL、无法确认实际主体时标记待核验，不能成为合格候选。同一主体的多个
页面保留为多份证据，不重复建档。去重不能只依赖平台公共域名。

八项核验固定检查产品类型、材质、尺寸、型号、数量档、MOQ、计价单位和
币种，并要求完整 EvidenceSnapshot 与无未知项的匹配解释。诱导性最低价、
模糊区间、缺数量档、单位或币种均进入结构化拒绝原因。合格候选最多三个，
被拒候选仍保存为规则校准数据。

`research_only` 运行不得调用联系人、邮件、采购、询价或客户报价工具。

## 十一、停止原因与失败语义

至少区分：

```text
approval_required
quota_status_unknown
paid_usage_enabled
quota_exhausted
provider_timeout
provider_rate_limited
page_access_forbidden
login_or_captcha
unsafe_redirect
no_search_results
no_verifiable_supplier
no_qualified_candidate
reconciliation_required
opportunity_required
```

只有计划已授权、允许的搜索和页面预算已执行完，并且确实没有合格供给时，
才能回流 `NO_SUPPLY_FOUND`。额度、网络、验证码、登录墙或访问限制代表
“未知/受阻”，不能显示成“没有供应商”。

## 十二、API 与界面

API 分开查询和命令：

```text
GET  /sourcing-cases
GET  /sourcing-cases/{id}
GET  /sourcing-cases/{id}/ladder-checks
GET  /sourcing-cases/{id}/candidates
GET  /sourcing-cases/{id}/public-search-plan

POST /sourcing-cases/{id}/public-search-plan
POST /sourcing-cases/{id}/public-search-plan/confirm
POST /sourcing-cases/{id}/run
POST /sourcing-cases/{id}/review
POST /sourcing-cases/{id}/reconcile-uncertain-request
```

创建或修改计划不执行搜索；`run` 再次核验计划哈希、确认状态和实时额度。
普通 API 不提供绕过额度检查的选项。不确定请求只能在人工核对后恢复。

现有 Web 增加：

- 需求详情中的自动 Case、完整度、Provenance 与停止原因；
- 寻源中心的七级阶梯与逐级证据；
- 公开寻源确认页的国家、品类、查询/页面上限、免费额度与付费状态；
- 最多三个候选的八项核验、规格差异、INDICATIVE 价格和原始快照；
- 一个主候选、最多两个备选的人工选择区；
- 产品中心的 `source_only` 筛选、来源链和“不可用于客户报价”标记；
- Run Center 的请求、页面、额度、停止原因、人工确认与恢复状态。

前端类型全部从 OpenAPI 重新生成，不维护手写重复接口。

## 十三、可观测性

记录可验证事实，不生成加权综合分或模型概率：

- Case 创建量与进入公开寻源的比例；
- 各阶梯首次找到合格供给的数量；
- 每个 Case 的查询数、页面数、额度和模型成本；
- 待确认、待核验、待选择队列深度与最长等待时间；
- 合格候选数、拒绝原因和 `NO_SUPPLY_FOUND` 原因；
- 从 `Validated Need` 到成本交接的耗时。

这些事实为后续 NeedCluster 排序、70/30 分配和北极星指标提供数据，但本批
不在没有真实结果数据时提前实现自适应算法。

## 十四、实施切片

1. 公共契约与迁移：V2 事件、强类型 DTO、Case/计划/阶梯/审核/来源表；
2. sourcing 域实现：仓储、服务、状态、门槛、核验和三候选上限；
3. 内部阶梯：接入产品与供应商公共服务，执行 1–5；
4. 公开寻源：计划审批、额度预留、Tavily、安全读取和恢复；
5. 候选与产品卡：事实/推断分离、快照、去重和 `source_only` 卡；
6. 人工审核与成本交接：主候选、备选和 V2 交接事件；
7. API、Web、OpenAPI 类型、HANDBOOK、ROADMAP、ADR 与验收报告。

每个切片先写能证明相应边界的失败测试，再实现到通过。耗时较大的后端
全量回归、前端构建和浏览器验收在全部切片接线后集中执行。

## 十五、测试与验收

### 15.1 契约、状态与租户

- 重复 `NeedValidated` 只生成一个 Case；
- 完整度不足或关键字段缺 Provenance 时不开 Case；
- 阶梯 1–5 不能跳级，内部命中时 Tavily 调用为零；
- 非法状态转换、过期人工审核和跨租户读写全部拒绝；
- 状态、审计和 Outbox 保持事务一致。

### 15.2 额度与安全

- 未确认、计划已修改、付费开启、额度未知/不足时外部调用为零；
- 并发耗尽、超时、限流和重启不产生重复请求或付费回退；
- 结果不确定时保留预留，只有人工核对后才能恢复；
- 私网、危险重定向、登录墙、验证码和禁止页面不能成为证据；
- 凭证、完整原文和敏感内容不进入日志或事件；
- `research_only` 下联系人、发信、采购和报价工具调用均为零。

### 15.3 候选、产品与成本

- 搜索摘要、自述和模型推断不能升级成核实事实；
- 八项不完整、证据缺失、诱导价或模糊价的候选不能合格；
- 合格候选与 `source_only` 卡均不超过三个；
- 重复事件不重复建候选或产品卡；
- 未人工选择时成本调用为零；
- 选择后只为主候选创建一张 `ESTIMATED` 成本表；
- 同 Need 没有真实 Opportunity 时保持 `candidates_ready` 并返回
  `opportunity_required`，成本调用仍为零；
- 公开价格不能进入客户可见报价或变成 `QUOTED`。

### 15.4 最终门禁

- `python3 scripts/check_boundaries.py`；
- 后端相关测试与全量回归；
- 前端测试、OpenAPI 类型检查和生产构建；
- 浏览器完成计划确认、候选审核、主备选择和成本交接；
- 配置真实免费 Tavily 账户后读取公开供应商页面，报告真实网页证据；
- 未配置真实密钥时将真实联网验收记为 `not_run`，与受控 fixture 结果分开。

## 十六、文档与 ADR

实现时同步：

- `ROADMAP.md`：标记该子项目的实际范围与状态，不把它写成整个 Phase 2
  完成；
- `HANDBOOK.md`：增加运行、审批、恢复和验收步骤，并修正成本/报价批次
  已合并但文档仍写“尚未合并”的陈旧描述；
- sourcing、products、costing、workflow、connector、gateway 和 UI 就近
  `AGENTS.md` 中受影响的模块规则；
- ADR：记录完整度 3 的统一门槛、V2 事件拆分、旧事件兼容、强类型跨域
  契约和 INDICATIVE 字段迁移。

本规格完成后，Phase 2 仍有独立子项目：真实 Tavily/模型/生产调度验收、
NeedCluster 驱动的寻源排序、多提供商联系人瀑布、70/30 自适应分配和接管
队列背压。后四项需要分别设计，其中联系人瀑布必须等第二个真实联系人
提供商存在；自适应分配必须等真实策略结果数据足够；背压的自动预算调整
还依赖后续额度/配额契约，不能用占位算法冒充完成。
