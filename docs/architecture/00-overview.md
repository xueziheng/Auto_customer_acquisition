# 总体架构

## 形态：模块化单体 + 独立 Worker

一个代码库、一套依赖、一份数据库 schema，但按业务域严格分层，并把长耗时和高风险工作拆成独立进程。不要一开始拆几十个微服务——理由见 [ADR 0005](../adr/0005-modular-monolith.md)。

分层与依赖方向（反向导入即违规）：

```text
┌─────────────────────────────────────────────────┐
│  apps/          API 与各 Worker、Web 前端        │
├─────────────────────────────────────────────────┤
│  workflows/     长流程状态机                     │
│  agent-runtime/ Agent 能力、上下文、护栏          │
├─────────────────────────────────────────────────┤
│  domains/       17 个业务域（域间零直接依赖）      │
├─────────────────────────────────────────────────┤
│  shared/        事件、Provenance、Money、租户      │
└─────────────────────────────────────────────────┘

横切设施（被上层调用，自身不含业务规则）：
  tool-gateway/          所有外部动作的唯一出口
  notification-gateway/  统一通知出口
  artifact-store/        原始资料与不可变存储
  connectors/            外部系统适配器
  skills/                技能 manifest 与 prompt 资产
```

---

## 八个进程（七个已实现＋一个未来桌面进程）

| 进程 | 职责 | 为什么独立 |
|---|---|---|
| `apps/api` | HTTP API，服务 Web 前端 | 需要低延迟，不能被长任务阻塞 |
| `apps/web` | Vue 3 前端 | 独立构建产物 |
| `apps/agent-worker` | 执行 Agent 任务（模型调用、技能编排） | 耗时长、成本高、需要独立限流与重试 |
| `apps/scheduler-worker` | 扫描状态机表、推进到期流程、发出定时任务 | Phase 1 的工作流引擎驱动器，必须单独可控 |
| `apps/browser-worker` | Playwright 浏览器操作 | 资源重、崩溃风险高，必须隔离；每租户/账号/Run 独立 BrowserContext |
| `apps/notification-worker` | 投递站内、邮件、macOS、企业微信通知 | 外部渠道不稳定，失败重试不应阻塞主流程 |
| `apps/email-feedback-worker` | Gmail DSN typed 读取与整页反馈提交 | OAuth/HTTP 隔离；按 tenant＋mailbox 单副本，崩溃不回退 cursor |
| `apps/desktop-tauri` | macOS 本地能力（Phase 3，现不建目录） | 见 [ROADMAP](../../ROADMAP.md) |

所有 Worker 共享同一份 `domains/` 代码，靠数据库和事件总线协作，不互相调用 HTTP。

---

## 数据链路在架构上的落点

```text
Demand Signal      ← connectors/（web-search、公开页面）→ domains/demand
      ↓                agent-runtime/demand-intelligence 提假设
Need Hypothesis    → domains/demand
      ↓                domains/contacts 验证可达性（硬边界 6）
                       domains/sending-identity 提供合规发件身份
触达               → domains/outreach + workflows/outreach-campaign
      ↓                connectors/gmail 经 tool-gateway 发送
投递反馈           → apps/email-feedback-worker + workflows/email-feedback
      ↓                hard bounce 抑制联系人并写身份信誉；soft bounce 只记 receipt
回复识别           → domains/conversations + workflows/reply-qualification
      ↓
Validated Need     → domains/demand（带 Provenance，硬边界 4）
      ↓
Trade Opportunity  → domains/opportunities（硬边界 3 推导置信度）
      ↓
Supply Match       → domains/products / suppliers / sourcing
      ↓
Quote              → domains/costing（硬边界 2、7）→ domains/quotations
      ↓                domains/approvals 人工审批
Human Execution    → domains/employees、commitments、notification-gateway
      ↓
Deal Outcome       → domains/opportunities（结构化 Loss Reason）
```

Phase 1 只自动化到「人工接管」这一步，寻源与报价由人工完成，但数据结构和门禁先建好。

---

## 三条不可绕过的通道

架构的安全性建立在「只有一条路可走」上，而不是靠每个调用点自觉：

1. **所有外部动作只经 `tool-gateway/`。** 模型和 Agent 拿不到任何凭证（硬边界 1）。新增工具只需加 manifest 和 handler，不改管线。
2. **域间通信只经 `shared/events`。** 域与域之间零直接导入。确需同步调用时，只能调对方 `service.py` 显式导出的接口。
3. **所有通知只经 `notification-gateway/`。** 渠道适配器可插拔，业务代码不知道通知最终走哪个渠道。

---

## Agent 在架构中的位置

用户只看到一个 **Trade Agent**。后台是一个 Trade Manager 加九个专业能力，而不是上百个 Agent。

**但 Manager 不编排流水线。** 发现 → 验证 → 寻源 → 报价这条流水线由确定性代码和 `workflows/` 驱动；大模型只在几个明确的点介入：理解老板指令、提需求假设、设计搜索策略、分析网页与文件、提取聊单信息、生成邮件、选择下一个该问的问题、比较候选、解释成本、识别风险。

让大模型驱动整条流程会同时得到慢、贵、不可复现三个问题。Agent 是入口和解释者，不是编排者。详见 [06-agent-runtime.md](06-agent-runtime.md)。

---

## 存储职责

| 存储 | 存什么 |
|---|---|
| PostgreSQL | 业务真相：全部结构化对象、状态机表、审计与 Provenance |
| pgvector | 语义检索：历史对话、需求、产品描述的向量 |
| Redis | 缓存、限流计数、幂等键短期占位 |
| S3 / MinIO | 原始资料：截图、PDF、Excel、网页快照、图片。经 `artifact-store/` 存取，不可变 |

**不引入图数据库**——「客户—需求—产品—供应商」看起来适合图，但初期关系表加 JSON 足够。等关系查询真正成为瓶颈再说，理由见 [ADR 0006](../adr/0006-postgres-not-graph.md)。

---

## 相关文档

- 业务对象与状态机：[01-domain-model.md](01-domain-model.md)
- 模块边界与四个插件点：[02-boundaries.md](02-boundaries.md)
- 权限模型：[03-permissions.md](03-permissions.md)
- 工具网关：[04-tool-gateway.md](04-tool-gateway.md)
- 数据平面与 Provenance：[05-data-plane.md](05-data-plane.md)
- Agent 运行时：[06-agent-runtime.md](06-agent-runtime.md)
- 发件身份与域名信誉：[07-sending-identity.md](07-sending-identity.md)
- 合规：[08-compliance.md](08-compliance.md)
- 触达 Campaign 与邮件反馈：[09-outreach-campaign.md](09-outreach-campaign.md)
- 打分与反馈闭环：[09-scoring-and-feedback.md](09-scoring-and-feedback.md)
- 数据库表清单：[10-database.md](10-database.md)
- 部署：[11-deployment.md](11-deployment.md)
