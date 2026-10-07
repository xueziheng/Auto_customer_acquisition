# workflows/sourcing_case/ —— 寻源流程（Phase 1 骨架 / Phase 2 自动）

## 触发

Phase 1：人工在寻源中心点「开案例」。Phase 2：需求达完整度 3 后按队列自动开。

## 旧 V1 兼容状态

```text
open_case（sourcing 域，校验完整度门槛）
→ check_ladder（匹配梯子 1–5 级逐级查产品与供应商域）
→ public_search（第 6 级：sourcing_agent + web 工具，历史 V1）
→ verify_candidates（核验清单 + 证据快照，最多 3 个合格）
→ complete_case（旧 `SourcingCaseCompleted` 兼容事件）
   或 fail_case（NO_SUPPLY_FOUND 回流）
```

## 关键约束

Phase 1 人工执行时**走同一批域服务接口**——数据结构与门禁一致，Phase 2 只是换掉推进者。NeedCluster Sourcing Admission 已交付，但它只排序尚未启动的 V2 Case；Catalog Product Proposal 仍是 ROADMAP 挂载点。

## NeedCluster Sourcing Admission

完整度达到 3 的 readiness 事件只能幂等建立一个 canonical V2 Case 和一条 durable Admission，不能在
event handler 内直接启动 Workflow。没有当前 boss-confirmed `sourcing_admission` Directive、自动准入关闭
或策略状态未知时必须零 claim、零 start。确认提案只改变策略；scheduler 每轮按 immutable priority
snapshot 的成员数降序、`ready_at` 升序和稳定 ID 领取精确 `batch_limit`，再逐 Case 使用 canonical
业务键启动/绑定 Run。

`NeedClusterMembershipChanged` 只能触发重新读取 Demand 当前事实，并刷新 matching waiting/blocked
Admission；事件自带 member count 不是排序权威。starting/admitted 的快照不改，Need/Case/数量/规格/
Provenance 永远不按簇合并。`NeedClusterFormed` 保持第二成员首次形成多成员簇的既有语义，本路径不消费。

runtime 重启、readiness 重放、自动准入和人工准入必须收敛到同一 Case/Run；租约、数据库 admission
guard、V2 subject 唯一索引和原始人工 `Idempotency-Key` 不能被绕过。首次人工 actor/key 必须持久且与
可轮换的 scheduler claim token 分离；结果不确定时先读取 Admission/Run，再由同 actor 用原键恢复。
0056 前 actor-only 历史行不得猜造 key，人工恢复失败关闭但 scheduler 可继续 canonical 收敛。历史 V1、
0055 前已有 V2 Run 及旧提案继续按原版本解释，不得自动回填或重排；显式历史
回填只补 Admission/immutable snapshot，仍不启动 Workflow。

`DirectiveActivated`、`SourcingCaseOpened` 与 `OpportunityQualified` 当前采用具名、tenant-bound lifecycle
audit acknowledgement，只确认该组合已消费事件，不能创建联系人、采购、报价、外部调用或业务状态推进。
不得扩展成全局 no-handler 宽容策略。

## V2 受控公开寻源

V2 固定八步：`check_ladder → await_public_plan → public_search → verify_candidates → prepare_candidates → await_product_cards → await_review → handoff_costing`。`public_search` 不允许引擎自动重试；它只恢复持久 search receipt/page attempt，遇到额度 unknown/paid/exhausted、unsafe page 或 uncertain 都必须进入带 stop reason 的等待。Verified 只唤醒产品卡投影，Ready 只说明卡集完成；review submit 与 boss confirm 是两个事实。Opportunity 缺失时必须同时持久化 Case stop 与 Run 等待上下文为 `opportunity_required`；Opportunity 补齐后只接受该 Case/Run 的精确 retry，重放/重启不能再产生第二次成本交接。

租户 guard 必须先于任何 Workflow 写入：错误 tenant 的 retry/review POST 必须 403，且 owner tenant 的 Case、Run、CostSheet、ToolCall 全部不变。受控验收需真实关闭并重建 scheduler runtime 后再做该精确 retry/replay，不能以同进程重复调用替代。
