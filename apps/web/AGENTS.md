# apps/web/ —— Web 前端（Phase 1 浅骨架）

## 技术

Vue 3 + TypeScript + Vite + Ant Design Vue。**类型从 API schema 生成**（openapi-typescript），不手写重复定义——手写的类型会和后端漂移。

## 页面与目录（16 页对应 16 个 view 目录）

```text
src/
├── views/
│   ├── command-center/      自然语言指挥（老板确认提案的三栏对照）
│   ├── demand-radar/        信号/假设/簇（推断必须视觉区分于事实）
│   ├── customer-discovery/
│   ├── campaigns/           含发件身份状态卡（预热进度、熔断状态）
│   ├── inbox/
│   ├── crm/                 机会看板 + 接管队列（按等待时长排）
│   ├── products/            按角色渲染对应视图（数据已由后端裁剪）
│   ├── sourcing/            Phase 1 人工寻源操作台
│   ├── costing-quotes/
│   ├── team/
│   ├── work-uploads/        拖拽上传 + 提取结果对照确认
│   ├── commitments/
│   ├── approvals/           一屏决定（审批包全文）
│   ├── runs/                Run 全景与证据链
│   ├── settings/
│   └── billing/             Phase 3 占位路由，显示"未开通"
├── components/              跨页面组件（ProvenancePopover 最重要：
│                            任何关键数字旁的"这从哪来"展开）
├── api/                     生成的客户端与类型
└── stores/                  Pinia
```

## 两条纪律

1. **前端不做权限判断的最终裁决**——它只按后端给的数据渲染。`can_current_user_decide` 这类判断后端算好传来。
2. **推断与事实的视觉区分是产品要求**：假设卡片必须有明显的「推断」标识与证据展开，不能和已验证需求长一样。

## Phase 1 范围

目录骨架 + 路由表。组件实现随 API 就绪逐页补。

## Sourcing V2 页面

只消费生成的 OpenAPI 类型。计划替换后必须让旧确认失效；展示事实、自述、推断、未知与 `INDICATIVE`/`QUOTED` 的差别。primary 至多一个、alternate 至多两个只是 UX guard，后端仍为最终裁决。`source_only` Product 必须显示来源链和“不可用于客户报价”；Run 页面只能显示安全 counters、quota、stop/reconciliation，不能显示 workflow context、网页原文、联系人或凭证。

## NeedCluster 寻源准入

- Command Center 的准入提案必须显式提交 `cluster_ranked`、自动准入开关和每轮上限；创建或确认提案都不得显示为已启动 Workflow，确认后只展示生效 Directive version。
- Sourcing Center 必须原样保留 API 返回顺序，分开显示“等待准入”和“处理中”；需求簇只解释排序，一个 Need 始终对应一个 Case，不能写成合并订单。
- 四类 admission 响应不是原子快照：跨状态出现重复身份时准入区必须 fail-closed；`/sourcing-cases` 只能作为独立、中性的 Case 工作台展示，不得用 admission 差集推断 Case 是历史记录或正在处理。
- 人工准入只按后端 `can_current_user_manual_start` 渲染；确认对话框中的原始 Idempotency-Key 只随请求发送，失败恢复时不得换键，也不得在 UI 或日志展示。
- admission 详情只显示安全不可变排序快照和 admitted actor/time；禁止渲染 claim token、租约、`requested_by`、完整 Workflow context 或底层异常。
