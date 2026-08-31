# workflows/sourcing_case/ —— 寻源流程（Phase 1 骨架 / Phase 2 自动）

## 触发

Phase 1：人工在寻源中心点「开案例」。Phase 2：需求达完整度 3 后按队列自动开。

## 主要状态（Phase 2 全自动版，Phase 1 人工推进相同状态）

```text
open_case（sourcing 域，校验完整度门槛）
→ check_ladder（匹配梯子 1–5 级逐级查产品与供应商域）
→ public_search（第 6 级：sourcing_agent + web 工具，Phase 2）
→ verify_candidates（核验清单 + 证据快照，最多 3 个合格）
→ complete_case（发布 SourcingCaseCompleted → costing 起估算表）
   或 fail_case（NO_SUPPLY_FOUND 回流）
```

## 关键约束

Phase 1 人工执行时**走同一批域服务接口**——数据结构与门禁一致，Phase 2 只是换掉推进者。队列排序 Phase 1 按 opened_at，Phase 2 按需求簇规模（挂载点见 ROADMAP）。

## V2 受控公开寻源

V2 固定八步：`check_ladder → await_public_plan → public_search → verify_candidates → prepare_candidates → await_product_cards → await_review → handoff_costing`。`public_search` 不允许引擎自动重试；它只恢复持久 search receipt/page attempt，遇到额度 unknown/paid/exhausted、unsafe page 或 uncertain 都必须进入带 stop reason 的等待。Verified 只唤醒产品卡投影，Ready 只说明卡集完成；review submit 与 boss confirm 是两个事实，Opportunity 缺失时只能等待精确 retry 事件。
