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

Phase 1 人工执行时**走同一批域服务接口**——数据结构与门禁一致，Phase 2 只是换掉推进者。NeedCluster 排序仍是 ROADMAP 挂载点，不是 V2 已交付的队列策略。

## V2 受控公开寻源

V2 固定八步：`check_ladder → await_public_plan → public_search → verify_candidates → prepare_candidates → await_product_cards → await_review → handoff_costing`。`public_search` 不允许引擎自动重试；它只恢复持久 search receipt/page attempt，遇到额度 unknown/paid/exhausted、unsafe page 或 uncertain 都必须进入带 stop reason 的等待。Verified 只唤醒产品卡投影，Ready 只说明卡集完成；review submit 与 boss confirm 是两个事实。Opportunity 缺失时必须同时持久化 Case stop 与 Run 等待上下文为 `opportunity_required`；Opportunity 补齐后只接受该 Case/Run 的精确 retry，重放/重启不能再产生第二次成本交接。
