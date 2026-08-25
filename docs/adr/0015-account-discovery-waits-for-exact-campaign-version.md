# ADR 0015：账户发现等待精确 Campaign 版本激活

- 状态：已接受
- 日期：2026-08-26

## 背景

Phase 1 要求先发现并验证联系人，再由老板批准和激活 Campaign，最后才允许联系人进入发送
序列。原 `account_discovery` 在验证与分配后立即调用 Enrollment；Campaign 尚未激活时只能
失败，无法表达“联系人已验证、等待精确版本批准”的耐久状态。若调用方在激活后重启整个流程，
会重复消耗联系人 Provider 成本，也可能把修订后的 Campaign 版本与先前审核范围混为一谈。

该变化跨越 workflow 公共定义、Outreach Enrollment 请求和 scheduler outbox 组合，必须明确
版本绑定、重放与拒绝/取消/修订语义。

## 决策

1. `account_discovery` 升级为版本 2，在任何联系人 Provider 调用前通过 Outreach 公共服务读取
   Campaign，把 `campaign_id` 与正整数 `campaign_version` 固定写入 run context。
2. 联系人发现、法律依据记录、可达性验证和 owner 分配仍在审批前完成；只有 verified contact
   可进入 `await_campaign_activation`。该步骤使用 workflow engine 的 `WAITING_EVENT`，不轮询、
   不吞错，等待 metadata-only `CampaignStateChanged`。
3. Campaign 的 active、cancelled 和 revise-to-pending 状态变化与 Campaign 写入同一 Unit of Work
   发布 `CampaignStateChanged`。事件只含 tenant、Campaign ID、版本、状态和事件元数据，不含
   联系人、正文、凭证或客户数据。
4. dedicated scheduler 以 `AccountDiscoveryCampaignEventHandlers` 订阅状态事件：只把 exact
   Campaign ID 的 active 事件投递给等待 run，并要求事件版本、run 绑定版本和当前持久 Campaign
   版本三者完全一致；cancelled 取消相关 run；修订产生的新版本取消仍绑定旧版本的 run。Campaign
   approval 的 reject 通过既有 `ApprovalDecided` 加公共 Approval 重读，按
   `campaign:{id}:v{version}` 精确取消对应 run。
5. 等待 handler 收到 active 后必须再次通过 Outreach 公共服务读取 Campaign，同时核对 ID、版本
   和 active 状态；事件与当前持久事实任一不一致即 fail closed。重复事件由 workflow engine 的
   durable 事件指纹幂等处理，不会重复 Enrollment。
6. `EnrollmentCreateRequest.campaign_version` 是可空的加法字段；v2 账户发现必须填写。Outreach
   在 Campaign 行锁内先比较请求版本，再检查 active 并创建 Enrollment，关闭“等待检查通过后
   Campaign 又被修订”的竞态。非空版本同时属于 Enrollment 幂等内容，同 key 跨版本重放固定
   冲突；其他现有调用方未传版本时维持兼容行为。
7. worker 同时注册精确 v1 与 v2 definition。v1 保留原步骤、转换、handler ref，以及不要求
   `campaign_version` 的 legacy assign/enroll handler；新 start 由引擎选择最高版本 v2。不得把
   “部署前 drain 掉旧 run”当作兼容策略，因为宕机和长等待期间无法证明 drain 完整。
8. `CampaignStateChanged` 同时登记在显式 outbox registry、`domains/outreach/events.PUBLISHES`
   和领域 Agent 事件清单；序列化 round-trip 只允许安全 metadata。

## 理由

等待属于业务状态，不是测试夹具或 API 特例。使用 workflow `WAITING_EVENT` 可以在进程重启后
恢复，并避免轮询 Campaign 或重新调用收费 Provider。事件负责唤醒，Outreach 公共服务重读负责
最终事实判断；二者结合既保留 outbox 的耐久解耦，又不把事件当成授权或当前状态本身。

在 Enrollment 创建请求中携带精确版本，把最终竞态检查留在拥有 Campaign/Enrollment 事务的
Outreach 域内，不需要跨域 SQL、test-only endpoint 或绕过审批 provider。

## 放弃的选项

- **Campaign 未激活就让流程失败，激活后人工重跑。** 会重复 Provider 成本，且不能保证重跑
  仍针对原审批版本。
- **验证后直接创建暂停 Enrollment。** 会破坏“未激活 Campaign 零 Enrollment”的验收语义，
  也扩大 Outreach 状态机。
- **定时轮询 Campaign。** 增加数据库负载和延迟，且错误/修订状态容易被当作继续等待。
- **只信 active 事件，不重读 Campaign。** 不能抵御旧事件重放或事件投递与修订的竞态。
- **由 scheduler 直接写 Enrollment。** 绕过 Outreach 公共服务的 verified-only、active、版本、
  suppression 和幂等门禁。

## 兼容与部署顺序

本决策不新增数据库列或迁移。`CampaignStateChanged` 是新增事件类型，
`EnrollmentCreateRequest.campaign_version` 是可空加法字段；旧 Enrollment 行与旧调用方可继续读取
和运行。workflow v1 已有 run 由精确 legacy definition 与 handler 执行，新的账户发现 run 使用
最高版本 v2。真实 PostgreSQL 部署重启测试持久化一个 v1 run，再以同时注册 v1/v2 的新 engine
完成旧 run，并证明新 start 记录为 v2。

部署时先发布能解析新事件和请求字段的 shared/domain/workflow 代码，再发布已注册 v2 definition
及两个 scheduler handler 的 worker，最后启用从 Web 启动的新账户发现。若 scheduler 未注册
handler，outbox 会 fail closed；不得以忽略事件或直接入组作为降级方案。

## 后果与复审条件

每个账户发现增加两次 Campaign 公共重读和一个耐久等待步骤，换来精确审批版本、零提前入组、
可重放恢复与明确的取消语义。真实 Hunter/Gmail/provider 网络仍不在本 ADR 的受控验收范围。

只有在未来采用具备同等 durable event、exact-version compare-and-create 与拒绝/修订取消语义的
编排引擎时，才可替换当前 Postgres workflow 实现；安全门禁不得弱化。
