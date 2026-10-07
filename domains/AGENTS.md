# domains/ —— 业务领域层

## 定位

业务规则的**唯一归属地**。「什么算合格机会」「什么价格算低利润」「需求什么时候能进寻源」这类判断只写在这里。

上层（`apps/`、`workflows/`、`agent_runtime/`）负责编排和呈现，不做业务判断。发现自己在 router 里写 `if opportunity.value < 3000` 就说明规则放错了地方。

## 域间依赖：零

```text
禁止   domains/a  import  domains.b
允许   domains/*  import  shared.*
```

这是全库最容易被违反、也最值钱的一条规则。违反它的代价不是抽象的整洁度，而是改一个需求要动七个文件。

**需要跨域协作时，两个选择：**

1. **发事件**（默认）—— 通知"某件事发生了"，对方自己决定要不要反应
2. **调对方 `service.py` 显式导出的接口**（例外）—— 必须立刻拿到返回值才能继续

选哪个：只是通知就发事件；报价前必须先读到成本这种，才用服务接口。默认选事件，同步调用会把两个域的可用性绑在一起。

**编排多个域的逻辑不属于任何单个域**，放 `workflows/`。

## 统一内部结构

```text
domains/<name>/
├── AGENTS.md        职责、状态机、依赖白名单、禁止事项
├── models.py        实体（持久化对象）
├── schemas.py       对外 DTO（跨域/对 API 暴露的形状）
├── service.py       领域服务 Protocol —— 这是本域的公共 API
├── repository.py    存储 Protocol
├── events.py        本域发布/订阅哪些事件（引用 shared/events/catalog）
└── errors.py        本域特有错误
```

`models.py` 和 `repository.py` 是内部实现，**其他域不得 import**。跨域只能碰 `schemas.py` 和 `service.py`。

## 十七个域

**深（核心链路与强约束，接口写全）**

| 域 | 职责 |
|---|---|
| `demand/` | 需求四层：Signal → Hypothesis → ValidatedNeed → Cluster |
| `opportunities/` | 贸易机会、硬门槛打分、Loss Reason |
| `sourcing/` | 寻源案例、候选核验、证据快照 |
| `costing/` | 成本表、三版本、Decimal 确定性计算 |
| `quotations/` | 报价版本化、禁止自动承诺清单 |
| `outreach/` | Campaign 边界、序列状态机、抑制名单 |
| `sending_identity/` | 发件身份隔离、预热、认证门禁、熔断 |
| `directives/` | 老板指令解析结果、版本化 |
| `approvals/` | 审批包、必须审批的变更注册表 |
| `compliance/` | 国家政策包、字段级 Provenance、独立审批与精确 action 判断 |

**浅（接口骨架 + 关键枚举写全）**

`organization/`、`employees/`、`prospecting/`、`conversations/`、`products/`、`suppliers/`、`commitments/`

## 每个域必须遵守

1. **状态机显式化。** 允许的转换写成表或常量，不散落在 if 里。非法转换抛 `InvalidStateTransition`，消息带当前态、目标态、允许的转换。
2. **`tenant_id` 出现在每个实体和每个 repository 方法签名里**（硬边界 8）。
3. **影响商业决策的字段带 `Provenance`**（硬边界 4）。
4. **事实与推断用 `FactualField` / `InferredField` 分开**（硬边界 5）。
5. **金额用 `Money`**（硬边界 2）。
6. **不导入任何外部 SDK。** 域是纯业务逻辑，外部调用走 `connectors/` 和 `tool_gateway/`。域里出现 `import httpx` 就是设计错了。

## 新增一个域

1. 建目录，照上面七个文件的结构
2. 写 `AGENTS.md`：职责、状态机、依赖白名单、禁止事项
3. 需要新事件就加到 `shared/events/catalog.py`（加事件不需要 ADR）
4. 在 `docs/architecture/02-boundaries.md` 的依赖表里登记
5. 若改了跨域契约，留 ADR
