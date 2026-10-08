# domains/products/ —— 产品域（浅）

## 职责

**Supply Capability Center（供应能力中心）**：公司供应侧的全貌。

按根总纲及 ADR0081，每家企业先完善并确认自己的产品资料，再用于本企业的客户探索。产品匹配仅形成需求假设，不能直接认定采购需求已验证。

## 三个供应池

```text
正式产品      规格/图片/供应商/成本/MOQ/交期/质量/可售市场全部确认
候选产品      来自历史寻源，未完全验证（source_only / partial / not_approved）
供应能力      没有固定产品但有能力：金属加工、注塑、包装、OEM、
              小批量定制、中国供应商整合
```

第三个池容易被忽略但很重要：贸易公司真正的资产常常不是某个 SKU，而是「能找到货、能整合供应商」这个能力本身。

## 三视图是权限边界，不是 UI 选项

```text
内部视图（boss/product/sourcing/finance）
    供应商、真实采购成本、内部利润、风险、历史问题
销售视图（sales）
    允许售价范围、卖点、MOQ、交期范围、常见问题、可发送图片
客户视图（对外）
    图片、名称、规格、可选型号、客户可见介绍、询价/样品入口
```

**内部视图泄漏到客户是永久性商业损伤**：客户知道了你的成本，以后每次谈判都从你的底价开始。所以裁剪在**序列化层强制**（三个不同的 View 类），不靠前端「不显示」——前端拿到了数据就等于泄漏了。

## 依赖白名单

```text
允许   shared.*        禁止   其他 domains/*、外部 SDK
```

## 事件

发布：无
订阅：`SourcingCandidatesVerified`（精确封存 generation → 幂等投影候选产品卡）

投影必须以同一 `case_version` + `candidate_set_hash` 绑定全部 Product、Supplier
Option 与最终 ready 迁移；`SourcingCandidatesReady` 是完整卡集冻结后的最终事实，
不是建卡请求。重复或并发投影按 Case+Supplier Candidate canonical source 收敛。

V2 投影的 Product 固定为 `source_only`，保留 canonical public evidence chain；它不可用于客户
报价或伪装成正式产品。公开 Candidate 的 `product_type`/`size` 仅在确定映射处转为本域
`product_category`/`size_spec`，未知必须继续未知。

## Phase 1 范围

三池模型、三视图 DTO、匹配查询接口（供匹配梯子前五级查询）。不做：淘宝式卡片 UI 细节（那在 apps/web）、多语言产品文案生成（agent_runtime）、自动铺货（明确永不做）。

## Catalog Product Proposal 边界

- 策略、评估、提案与培养 Case 归本域；Demand 只通过 workflow 显式映射的 Products DTO 提供事实，本域不得导入 Demand。
- 策略没有生产默认值。`minimum_distinct_accounts` 至少为 2；受控验收的 3 个客户只是显式测试数据，不是 Kenya 或任何市场的业务默认。
- 评估只使用固定顺序的确定性规则与固定中文 explanation code，不输出概率、模型解释或自由文本判断。损坏事实统一失败关闭，禁止把缺失或损坏值猜成 0。
- Catalog Product Proposal 只是内部培养建议。批准后的唯一效果是创建一个 `queued` 培养 Case；不得创建或升级 Product、修改候选池状态、启动寻源、联系供应商、询价、报价或发送。
- 策略和提案的审批决定只能由 workflow 以可信 Products 输入应用；HTTP 不得接受客户端自报 tenant、actor、facts hash、Provenance、审批人或状态。
- 安全视图不得包含创建幂等键、请求 hash、原始 Provenance、客户原话、供应商联系人、价格或 workflow context。
- 非硬门槛事实缺失仍显式为 `unknown`，只是不阻断整体通过；不得为了让提案通过而把未知伪写为 `not_required`、0 或已确认。
- 培养审批所引用的 conversation/web/upload Evidence 必须是可路由定位符；测试数据也不得绕过该契约。
- 同一 facts hash 的事件重放、审批重投和 runtime 重启必须收敛到同一 evaluation、proposal、Approval 和 Case；批准前必须重读当前 Demand 事实，hash 已变只能转 `stale`。
- `CatalogCultivationQueued` 是 metadata-only 事实，不得在本域偷偷补外部动作；下游消费者未独立设计和授权前，培养队列就是终点。


## 企业共享资料

knowledge_* 承载本企业上传原件绑定、持久处理、证据草稿和人工确认；在职 boss/sales 共享，只有当前 boss 确认。模型草稿不能自动创建正式产品或报价。原文事实逐项绑定引文与来源，推断独立；租约过期或模型结果未知不得自动重试。Obsidian 只是可重建的 Markdown 投影，同步重试不再次调用模型。
