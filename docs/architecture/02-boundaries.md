# 模块边界与解耦机制

这份文档回答一个问题：**以后加功能时，怎么保证不用大改？**

答案是把变化集中到四个插件点，并让依赖只有一个方向。任何需要修改核心管线才能加上的功能，说明设计走偏了——先回来讨论，不要硬改。

---

## 一、依赖方向

```text
apps  →  workflows / agent-runtime  →  domains  →  shared
```

- **反向导入即违规。** `domains/` 不得导入 `apps/`、`workflows/`、`agent-runtime/`。
- **`shared/` 是叶子。** 它只定义契约，不导入任何上层，也不含业务规则。
- **横切设施**（`tool-gateway/`、`notification-gateway/`、`artifact-store/`、`connectors/`）被上层调用，自身不含业务规则，也不导入 `domains/`。

Phase 1 引入 `import-linter` 自动检查这些规则；在自动检查落地前，规则同样有效，靠 review 守。

### 为什么严格

反向依赖一旦出现，业务规则就会散到 API 层和 Worker 里。届时「改一个需求要动七个文件」不是因为业务复杂，而是因为边界烂了。

---

## 二、域间零直接依赖

`domains/a` **不得** `import domains.b`。这是最容易被违反、也最值钱的一条规则。

跨域协作只有两条合法路径：

**路径一：事件（首选）**

```python
# domains/demand/service.py —— 发布
bus.publish(NeedValidated(need_id=..., tenant_id=..., evidence_level=...))

# domains/opportunities/events.py —— 订阅
# 订阅 NeedValidated，据此评估是否创建 Trade Opportunity
```

事件定义全部集中在 `shared/events/catalog.py`。**事件即契约**——改事件字段等于改公共 API，必须留 ADR。

**路径二：显式导出的服务接口（仅当确实需要同步返回值）**

```python
# 允许：调用对方 service.py 里显式声明为公共接口的 Protocol
# 禁止：导入对方的 models.py、repository.py、内部函数
```

### 判断用哪条

需要立刻拿到结果才能继续（例如报价前必须先读到成本）→ 服务接口。
只是通知「某件事发生了」，对方自己决定要不要反应 → 事件。

**默认选事件。** 同步调用会把两个域的可用性绑在一起。

---

## 三、四个插件点

加功能只加文件，不改核心：

### 1. 新的外部系统 → `connectors/`

```text
新建 connectors/<name>/
  ├── AGENTS.md      能力范围、合规约束、密钥归属
  ├── client.py      实现 Connector Protocol
  └── manifest.py    注册元数据
```

核心代码零改动。Connector 只做协议转换，**不含业务规则**——「什么时候该发邮件」属于 `domains/outreach`，「怎么调 Gmail API」属于 `connectors/gmail`。

### 2. 新的技能 → `skills/`

增加一份 manifest（含 triggers、inputs、outputs、allowed_tools、blocked_tools、evidence_required、risk_level）加 prompt 资产。`skill-router` 按 trigger 自动发现，不需要改路由代码。

### 3. 新的工具 → `tool-gateway/`

增加一份工具 manifest（权限、风险级、成本类、是否需审批）加一个 handler。检查管线不动。

### 4. 新的通知渠道 → `notification-gateway/`

实现渠道适配器 Protocol 并注册。业务代码不知道通知最终走哪个渠道。

---

## 四、域内统一结构

每个 `domains/<name>/` 的文件职责固定，便于跨域阅读：

| 文件 | 职责 | 可被谁导入 |
|---|---|---|
| `AGENTS.md` | 职责、状态机、依赖白名单、禁止事项 | — |
| `models.py` | 领域实体 | **仅本域** |
| `schemas.py` | 对外 DTO | 上层与其他域 |
| `service.py` | 领域服务 Protocol（公共接口在此显式声明） | 上层与其他域 |
| `repository.py` | 存储 Protocol | 仅本域与依赖注入装配处 |
| `events.py` | 本域发布/订阅哪些事件 | 上层装配处 |
| `errors.py` | 本域错误类型 | 上层与其他域 |

当前登记的深域还包括 `domains/compliance/`：它独立拥有 tenant-scoped 国家政策版本、字段级
Provenance、append-only activation 与结构化政策判断。它只依赖 `shared.*`；上层及 Tool
Gateway 只能使用 `domains.compliance.schemas` 和 `domains.compliance.service`，不得读取其
models 或 repository。国家政策不并入 Company Playbook，也不在 Gateway 复制一份规则。

**`models.py` 和 `repository.py` 是私有的。** 其他域看到的只能是 `schemas.py` 的 DTO 和 `service.py` 的接口。这样换存储实现、改内部实体都不会外溢。

---

## 五、契约先行

跨模块的数据结构只能定义在两处：

- `shared/schemas/` —— 全局契约（Provenance、Money、TenantId、强类型 ID）
- `domains/<name>/schemas.py` —— 该域对外的 DTO

**不要**在 `apps/api` 里定义业务结构，也不要在 Connector 里定义业务结构。API 层的请求/响应模型应引用域 DTO，只在需要时做字段裁剪（例如产品的三视图）。

改动跨模块契约必须留 ADR。

---

## 六、横切设施的边界

| 设施 | 是什么 | 不是什么 |
|---|---|---|
| `tool-gateway/` | 外部动作的唯一出口与检查管线 | 不是业务逻辑的家。「该不该发」在域里判断，「能不能发」在这里检查 |
| `notification-gateway/` | 通知投递与渠道适配 | 不决定「什么值得通知」 |
| `artifact-store/` | 原始资料的不可变存取 | 不做内容理解 |
| `connectors/` | 协议转换 | 不含业务规则，不直接写业务表 |
| `agent-runtime/` | 模型能力、上下文裁剪、护栏 | **不编排业务流水线**（那是 `workflows/` 的事） |
| `workflows/` | 长流程状态推进 | 不含领域规则，只调用域服务 |

---

## 七、常见违规与正确做法

| 违规 | 为什么错 | 正确做法 |
|---|---|---|
| `domains/opportunities` 导入 `domains/demand` 的 `models.py` | 域间直接依赖，改一个动两个 | 订阅 `NeedValidated` 事件，或调 demand 的 service 接口 |
| 在 `apps/api` 的 router 里写打分逻辑 | 业务规则漏到应用层，Worker 里就用不上了 | 打分放 `domains/opportunities/service.py` |
| Connector 直接写 `validated_needs` 表 | 绕过域规则与 Provenance | Connector 返回数据，由域服务落库 |
| Agent 直接调 Gmail SDK | 绕过 Tool Gateway，违反硬边界 1 | 经 `tool-gateway/` 的工具调用 |
| 新增通知渠道时改 `notification-gateway` 的分发函数 | 每加一个渠道都要改核心 | 实现适配器 Protocol 并注册 |
| 在 `shared/` 里放业务判断 | `shared` 是契约层，放了业务就人人依赖 | 业务判断回到对应域 |

---

## 八、自检清单

改动合并前问自己：

- 有没有新增反向导入或跨域导入？
- 新功能是通过四个插件点加的，还是改了核心管线？
- 跨模块数据结构是否只定义在 `shared/schemas` 或域的 `schemas.py`？
- 外部动作是否都走了 Tool Gateway？
- 是否给新字段带上了 Provenance 和 `tenant_id`？

## ADR0070 内置 Agent

`domains/assistant` 拥有员工私有 Agent Session/Agent Turn，不拥有客户会话、研究提案或业务对象。
只依赖 shared；当前业务读取和多轮编排位于 agent_runtime/assistant、workflows/assistant，
通过公开服务消费其他域。模型调用共享契约位于 shared/schemas/model_invocation.py。
`model.generate` 是 Gateway 插件；DeepSeek Connector 不被域或 Agent 直接导入。
