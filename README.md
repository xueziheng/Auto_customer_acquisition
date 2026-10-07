# TradeOS Agent

以自主发现需求和客户为核心的跨境贸易智能操作系统。

系统的核心逻辑不是「拿着公司现有产品去找买家」，而是持续观察市场、发现真实需求、通过沟通验证需求，再调动公司产品、供应商网络和外部寻源能力去满足它。中心对象是**需求**与**贸易机会**，不是客户名单，也不是产品目录。

---

## 从哪里开始读

| 你想知道 | 看这里 |
|---|---|
| 项目规矩与硬边界 | [AGENTS.md](AGENTS.md) ← **先读这个** |
| 怎么动手实现 | [HANDBOOK.md](HANDBOOK.md) ← **要写代码就读这个** |
| 交接给 Windows / WSL2 同事 | [安装、启动与交接说明](docs/operations/windows-wsl2-handoff.md) |
| 术语的准确含义 | [GLOSSARY.md](GLOSSARY.md) |
| 现在做什么、不做什么 | [ROADMAP.md](ROADMAP.md) |
| 总体架构 | [docs/architecture/00-overview.md](docs/architecture/00-overview.md) |
| 业务对象与状态机 | [docs/architecture/01-domain-model.md](docs/architecture/01-domain-model.md) |
| 模块边界与如何加功能 | [docs/architecture/02-boundaries.md](docs/architecture/02-boundaries.md) |
| 关键技术决策的理由 | [docs/adr/](docs/adr/) |

进入任何子目录工作前，先读该目录的 `AGENTS.md`。

---

## 当前状态

**本机受控 Web 核心闭环已验收（2026-09-06）。** 原 API、scheduler、notification、Web 四进程可从独立环境启动；研究到 Signal/Hypothesis、独立获批触达、单 Provider 联系人验证、受控发送与回复、客户原话/Provenance、Need/Opportunity 和真人接管已贯通。研究不会自动转触达。

这是合成外部输入下的工程交付。48e4465 后端完整9318通过；7e10383仅修复两份测试的审批身份，完整主链1通过；Web411通过。具体版本、失败历史和 A1–A10 见[正式验收](docs/acceptance/2026-09-05-web-core-completion.md)，不可把不同轮次数量相加。

Phase 2 已交付研究、寻源、成本报价和 Catalog 提案等受控切片，范围见[路线图](ROADMAP.md)。Mac 统一入口未配置完整报价/自动寻源准入；完整报价和 PDF 由独立 Linux 环境验收，两者不是同一 Need。Catalog 终点是 queued 培养 Case。

**多人共享部署未验收。** 本机角色切换是开发身份，不是真实登录。真实 Tavily/Hunter/Gmail/供应商、发件信誉与实际接管 SLA、成本和真实客户验证仍未验收；通用 Agent/Browser 任务源、Catalog 培养消费者及桌面端未实现。不得由受控测试推导 Phase 1 真实运营达标或整个 Phase 2 完成。

---

## 本地开发

本机受控 Web 可用 `.venv/bin/python scripts/run_web_core_controlled.py` 启动独立 API、scheduler、notification、Vite 和新建 PG/MinIO。它不读取 `.env` 或既有业务库，初始业务待配置；角色演练、依赖安装、停止与未实现能力见 [受控 Web 操作说明](docs/operations/web-core-local.md)。完整受控回复链已验收；多人部署未完成。静止 owned 数据备份恢复演练和限制也见该说明。

下面是独立的常规开发依赖编排，不是受控入口的前置步骤，也不是共享部署runbook。
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
| 桌面端 | 未来 Tauri 2；当前仅[扩展契约](docs/architecture/12-client-capability-boundaries.md)，无实现 |

架构形态是**模块化单体 + 独立 Worker**，不是微服务。职责与依赖边界见[总体架构](docs/architecture/00-overview.md)。
