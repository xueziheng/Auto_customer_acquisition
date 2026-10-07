# ADR 0023：拆分寻源候选核验与最终就绪事件

- **状态**：已接受
- **日期**：2026-08-30

## 背景

Phase 2 设计同时要求“候选产品卡生成完成后 Case 才进入 `candidates_ready`”和
“产品域消费 `SourcingCandidatesReady` 生成产品卡”。若沿用一个事件，生产者必须在
产品卡不存在时宣称候选已经就绪；但产品卡与 `SourcingSupplyOption.product_id` 又受真实
产品外键约束，因此会形成事件时序环：没有 Ready 不能建卡，没有卡又不能发布 Ready。

此外，旧入口相信调用方传入的 Option/Candidate IDs，可能遗漏仓储中的合格选项，或在
Supplier Candidate 尚未绑定产品卡时把 Case 提前推进。此时人工审核看到的不是完整冻结
集合，后续成本交接也无法证明选择范围。

## 决策

增加过去式事实 `SourcingCandidatesVerified`。它携带 Case ID、sourcing 仓储重建出的
精确排序合格 Supplier Candidate IDs、封存后的 Case 版本与确定性集合哈希，不改变 Case
状态。候选全集、集合哈希、核验时间、Case CAS 与 Outbox 在同一事务提交；封存后不再接受
新候选。相同 generation 重试返回相同事实且不重复 Outbox，集合不同则拒绝。Task 11 产品投影消费该事件，幂等
创建 `source_only` 产品卡，并通过 SYSTEM-only sourcing 服务接口登记真实 ProductId 与
Supplier Candidate 的 Supply Option；登记与最终 readiness 都必须回传并核对事件的版本和
集合哈希，过期投影失败关闭。

只有产品卡和全部合格 Option 已存在后，workflow 才调用最终就绪入口。该入口重新读取
同租户 Case 的全部合格 Candidate 与 Option，要求调用集合无遗漏、无额外项，并验证每个
合格 Supplier Candidate 都有对应 Option；随后才原子推进到 `candidates_ready` 并发布
`SourcingCandidatesReady`。纯现有产品路径没有 Supplier Candidate，可直接按完整 Option
集合完成最终就绪。

本决策明确替代设计旧稿中“产品域消费 `SourcingCandidatesReady` 并生成产品卡”的冲突
文本。`SourcingCandidatesReady` 从此只表示产品卡与供给选项的完整冻结集合已经就绪。

## 理由

- 两个事件分别描述“供应商候选已核验”和“完整供给集合已就绪”，都是可审计的过去事实，
  不把命令伪装成事件。
- 产品域通过显式服务边界回写真实 ProductId；sourcing 域不导入产品域，也不创建占位 ID，
  现有复合外键继续证明租户与来源关联。
- 最终入口以仓储真相重建全集，调用方提供的 IDs 只用于精确一致性核对，不能缩小审核范围。
- 投影和 Option 注册都有稳定来源唯一键，Outbox 重投不会重复创建产品卡或 Option。
- Case CAS 封存消除了“发布者读到 A、并发提交 B、仓储成为 A+B 但事件只含 A”的时序窗口。

## 放弃的选项

**继续让产品域消费 `SourcingCandidatesReady`。** 少一个事件，但必须提前进入
`candidates_ready`，事件名称与事实不符，并迫使系统放宽产品外键或使用占位 ProductId。

**由 sourcing 域直接创建产品卡。** 可以在线性事务中完成，但造成业务域直接依赖，违反
单向依赖与域间零直接导入边界。

**把候选草稿直接作为 Supply Option。** 避免产品卡步骤，却混淆“网页候选”“内部产品”
和“可供人工审核的供给选项”，也让成本交接缺少稳定 ProductId。

## 后果

- 公共事件目录、Outbox 白名单和 Task 11 投影路由增加一个事件。
- Task 11 必须消费 `SourcingCandidatesVerified`，逐卡登记 Option，并在全部成功后显式完成
  readiness；每次调用必须携带事件 generation，不能再订阅 Ready 作为建卡请求。
- 最终 readiness 需要额外一次仓储全集读取，但换来可验证的完整审核范围。
- 旧 V2 草稿尚未发布，无需历史事件迁移；若外部消费者已基于旧草稿开发，必须改用新事件。

## 何时重新审视

只有当产品卡和 sourcing Case 能在不破坏域边界的同一聚合事务内持久化，或 Supply Option
不再要求真实 ProductId 时，才重新评估合并两个事件。单纯为了少一次事件投递不构成条件。
