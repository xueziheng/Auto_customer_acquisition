# ADR 0014：回复闭环使用显式耐久关联与受信证据验证端口

- 状态：已接受
- 日期：2026-08-25

## 背景

Phase 1 回复闭环必须把需求探索假设、触达 Enrollment、入站消息、Validated Need、
Opportunity 与 handoff 串成唯一、可重放的租户绑定链。原有公共 DTO 无法表达 Enrollment
来自哪个假设，应用层也不能按 Need 精确读取 Opportunity。首次实现因此增加了
`Enrollment.source_hypothesis_id`、`HypothesisView.validated_need_id` 与
`OpportunityService.get_by_need`。

首次实现还让调用方把 `MessageId` 与 `EvidenceLevel` 直接交给 Demand 服务。两者只是声明，
不能证明该消息真实存在、属于当前租户、为入站回复、已分类，或与指定 outbound、Enrollment、
account、contact 和 hypothesis 相连。这构成把合成 ID 升格为客户证据的后门。与此同时，
Opportunity 的 `account_name` 与 `country` 不能共用一条没有字段支持关系的通用网页证明。

这些 DTO 与服务方法跨越 app/workflow/domain 边界，按架构约束必须记录兼容策略与取舍。

## 决策

1. `Enrollment.source_hypothesis_id` 保持可空，以兼容人工或非探索来源的既有 Enrollment；
   Account Discovery 创建的 Enrollment 必须填写。回复闭环缺失该值时 fail closed。
2. `HypothesisView.validated_need_id` 保持可空，公开表达当前假设是否已晋升及精确 Need；
   应用层不得按 account 猜测 Need。
3. `OpportunityService.get_by_need` 作为只读、tenant-bound 公共查询保留。应用层用它做唯一
   Opportunity 的幂等恢复，不访问 Opportunity repository 或做跨域 SQL 投影。
4. Demand 公共契约使用 `CustomerReplyEvidenceClaim` 声明完整 typed 关联，并在服务构造时注入
   `CustomerReplyEvidenceVerifier`。公共记录方法不接受调用方生成的 proof 或 EvidenceLevel；
   verifier 从 Conversation/Outreach 持久事实返回 `VerifiedCustomerReplyEvidence`，Demand 再复核
   tenant、hypothesis、message、account 与最低客户证据等级后写入。部署组合只能注入应用层
   `TenantBoundCustomerReplyEvidenceVerifier`；测试 fake 只属于受信依赖边界。
5. `ProspectAccountView` 与 `AccountResolveRequest` 增加字段级 `field_provenance`。数据库 0038
   以非空 JSONB、默认空对象保存历史兼容值；新的 Demand Discovery 账户必须同时提供 `name`
   与 `country` 的精确证据。名称记录实际的确定性 URL host 提取器，国家记录实际模型版本和
   显式引用的逐字 signal；Opportunity intake 对缺失、推断来源或不完整字段 fail closed。
6. Opportunity 创建和真实 owner 分配后，应用层必须通过公开 `transition` 进入 `ASSIGNED`。
   每一步后重读公共视图，使「owner 已提交、状态未提交」等崩溃窗口可确定性恢复。

## 理由

把验证做成 Demand 的窄端口，Demand 域无需导入 Conversations 或 Outreach，仍能在领域入口
强制「客户明确说过」只能来自受信的耐久链。应用层适配器负责跨域编排，符合单向依赖和域间
零直接导入。完整 claim 让任何关联漂移都能精确拒绝，也避免仅凭 account 做歧义匹配。

字段级 provenance 保存真正支持每个商业字段的来源和提取者。0038 的空对象默认只用于读取
历史账户，不被回复机会 intake 当成合格证据，因此兼容迁移不会降低新闭环门槛。

## 放弃的选项

- **继续接收 `MessageId + EvidenceLevel`。** 接口简单，但任何调用方都能合成客户证据，破坏
  Validated Need 的定义。
- **由 Demand 直接读取 Conversations/Outreach。** 能验证事实，却造成域间直接依赖和事务
  边界耦合。
- **把 verifier proof 作为公共方法参数。** proof 仍可由普通调用方伪造；构造期注入把信任
  限制在 composition root。
- **按 account 查询“最近假设/Need/Opportunity”。** 重放时可能选中另一条业务链，且无法
  证明 outbound 与 Enrollment 的精确关系。
- **继续复用第一条网页证据。** 无法证明该网页同时支持名称与国家，也会丢失实际 extractor。
- **把 Opportunity owner 视为 assigned。** 混淆归属字段与状态机，崩溃后无法知道 handoff
  前置状态是否完成。

## 兼容与部署顺序

`source_hypothesis_id`、`validated_need_id`、`get_by_need` 和账户 provenance 字段为加法变更；
历史行仍可读取。`record_customer_reply_evidence` 是有意的不兼容源码契约收紧，所有调用方必须
在同一发布单元改用 claim，并在 composition root 注入真实 verifier，旧四参数调用必须失败。

先执行 0037/0038 migration，再部署能读取新增字段的 domain/app 代码，最后启用回复晋升与
Opportunity intake。回滚应用代码时不能让旧代码继续产生未验证客户证据；应先停止 reply
worker。0038 可降级删除字段，但降级会丢失新增字段 provenance，生产环境执行前必须备份。

## 后果与复审条件

回复晋升增加数次 tenant-bound 重读，换来明确的安全边界与崩溃恢复；Phase 1 的吞吐规模可
接受。真实 provider/model/mail 仍不在本 ADR 的验收范围。

当跨域事件投影能以同等强度提供完整关联、或 workflow 获得原子 saga/outbox 补偿能力时，
可重新评估同步 verifier 和逐步重读；在此之前不得退回合成 ID 或 account 猜测。
