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
