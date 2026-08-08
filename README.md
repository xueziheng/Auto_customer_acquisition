# TradeOS Agent

以自主发现需求和客户为核心的跨境贸易智能操作系统。

系统的核心逻辑不是「拿着公司现有产品去找买家」，而是持续观察市场、发现真实需求、通过沟通验证需求，再调动公司产品、供应商网络和外部寻源能力去满足它。中心对象是**需求**与**贸易机会**，不是客户名单，也不是产品目录。

---

## 从哪里开始读

| 你想知道 | 看这里 |
|---|---|
| 项目规矩与硬边界 | [AGENTS.md](AGENTS.md) ← **先读这个** |
| 怎么动手实现 | [HANDBOOK.md](HANDBOOK.md) ← **要写代码就读这个** |
| 术语的准确含义 | [GLOSSARY.md](GLOSSARY.md) |
| 现在做什么、不做什么 | [ROADMAP.md](ROADMAP.md) |
| 总体架构 | [docs/architecture/00-overview.md](docs/architecture/00-overview.md) |
| 业务对象与状态机 | [docs/architecture/01-domain-model.md](docs/architecture/01-domain-model.md) |
| 模块边界与如何加功能 | [docs/architecture/02-boundaries.md](docs/architecture/02-boundaries.md) |
| 关键技术决策的理由 | [docs/adr/](docs/adr/) |

进入任何子目录工作前，先读该目录的 `AGENTS.md`。

---

## 当前状态

**架构骨架阶段。** 目录结构、模块边界、接口契约和治理文档已就位，业务实现尚未开始——大部分函数体是 `raise NotImplementedError`，但签名、类型和职责说明是完整的、可据以实现的。

当前阶段为 Phase 1（需求验证闭环）：单租户运行、Postgres 状态机、寻源与报价先由人工完成。

---

## 本地开发

启动依赖服务（PostgreSQL + pgvector、Redis、MinIO）：

```bash
docker compose -f infra/docker-compose.yml up -d
```

复制环境变量样例后填入本地值。**不要把填好的 `.env` 提交到仓库**：

```bash
cp infra/.env.example .env
```

各进程的启动方式见 `apps/` 下对应目录的 `AGENTS.md`。

---

## 技术栈

| 部分 | 技术 |
|---|---|
| 后端 | Python 3.12+、FastAPI、Pydantic v2、SQLAlchemy 2.x |
| 前端 | Vue 3、TypeScript、Vite、Ant Design Vue |
| Agent | OpenAI Responses API、Agents SDK |
| 工作流 | Postgres 状态机（Phase 1）→ 视需要转 Temporal |
| 数据库 | PostgreSQL + pgvector |
| 缓存 | Redis |
| 对象存储 | S3 / MinIO |
| 浏览器 | Playwright |
| 桌面端 | Tauri 2（Phase 3） |

架构形态是**模块化单体 + 独立 Worker**，不是微服务。理由见 [docs/adr/0005-modular-monolith.md](docs/adr/0005-modular-monolith.md)。
