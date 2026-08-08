# Phase 0 · 工程基建实施计划

> **给执行者的说明**：本计划由零号任务（监督者 + 实现者）编写，覆盖 HANDBOOK「二、第 0 步：补齐工程基建」的全部交付。按 superpowers 计划约定逐任务打勾。**范围硬性限定在第 0 步工程基建**：不实现 shared 契约、不实现任何业务域、不改动既有业务骨架、不建业务迁移表。
>
> 每个任务独立可 review，格式统一为：文件 / 实现 / 失败预期与测试 / 验收命令 / commit / push。每任务提交前必须 `python3 scripts/check_boundaries.py` 全绿，每任务单独 commit、单独 push。

**目标**：让 `make dev` 起数据库、`make test` 跑通测试、`make check` 四件全绿（ruff / mypy / check_boundaries / pytest），为后续切片实现提供可运行的工程地基。

**执行环境**：`tradeos-py312`（conda，Python 3.12.13，零号任务已创建）。所有 Python 命令在该环境中执行；`python3 scripts/check_boundaries.py` 用系统 python3（结构检查脚本为纯 stdlib，与 Python 版本无关）。

---

## 全局约束

### 九条全局硬边界（AGENTS.md §三，本计划只收紧不放宽）

1. 模型永不接触凭证；一切对外动作只能经 `tool-gateway/`。
2. 金额只用 `Decimal` 确定性代码计算，禁止 `float`。
3. 置信度由代码依据证据等级推导，模型不得输出概率值，不存数值列。
4. 影响商业决策的字段必须带 Provenance。
5. 事实与推断在数据结构上分离。
6. 未验证可达性的联系人不得进入发送序列。
7. 只有 `quoted` 价格能进客户可见报价。
8. 所有表带 `tenant_id`，所有查询强制租户过滤。
9. 依赖方向单一：`apps → workflows/agent-runtime → domains → shared`，域间零直接导入。

### 本计划的技术约束

- **Python 3.12+**：`requires-python = ">=3.12"`；所有代码与验收命令运行于 `tradeos-py312`。
- **Decimal**：金额字段一律 Decimal/Money，不得出现 float（硬边界 2）。
- **tenant_id**：新增表与查询路径预埋租户维度（硬边界 8）；Repository 基类统一注入，不靠每个查询点自觉。
- **依赖方向**：新增代码不得引入反向导入、跨域导入或 SDK 越界（硬边界 9，`check_boundaries.py` 机检）。
- **每任务提交前**：`python3 scripts/check_boundaries.py` 必须全绿。
- **每任务单独 push**：一个任务一个 commit、一次 push，便于逐项 review。
- **本机准备不提交**：不提交任何机器路径、凭证或 `.env`；`.env` 由本机从 `infra/.env.example` 复制填充且永不提交。
- **不碰骨架**：不改 AGENTS.md 硬边界；不改动既有业务骨架文件内容。

### 已知基线（本计划不修复，仅在任务 1 用工程配置收敛）

- **ruff**：既有骨架报 243 处违规——230 处 EXE002（可执行位无 shebang，文件权限卫生）+ 13 处（骨架 stub 的预留导入 F401、`...` 占位 PIE790/PYI013、导入排序 I001、脚本别名 FURB167）。因禁止修改骨架，任务 1 通过 pyproject 配置忽略 EXE002 并按文件豁免其余 13 处。
- **mypy**：`shared/events/bus.py:28` 有 1 处既有类型错误（Protocol 类型变量不变性）。任务 1 通过 `[tool.mypy.overrides]` 按模块豁免。
- 上述豁免范围以「骨架期基线」为限，在配置注释中写明理由；不改变任何骨架文件内容。

---

## 前置条件（本机准备，不提交）

- [x] conda 环境 `tradeos-py312`（Python 3.12.13）已创建并核验（零号任务完成）。
- [ ] `docker compose -f infra/docker-compose.yml up -d` 可启动 postgres/redis/minio（任务 2 验收用到）。
- [ ] 根目录 `.env`：`cp infra/.env.example .env` 后填本地 `DATABASE_URL`（如 `postgresql+asyncpg://tradeos:tradeos_local@localhost:5432/tradeos`）。`.env` 永不提交。

---

## 任务列表

### 任务 1：pyproject.toml 工程配置

**文件**
- Create: `pyproject.toml`

**实现**
- 构建：setuptools，flat layout；`[tool.setuptools.packages.find]` 纳入 `shared*`、`domains*`、`tool_gateway*`、`notification_gateway*`、`artifact_store*`、`connectors*`、`agent_runtime*`、`workflows*`、`apps*`、`infra*`，排除 `tests` 与 `scripts`。
- `[project]`：`requires-python = ">=3.12"`；运行时依赖按 HANDBOOK：`fastapi`、`uvicorn`、`pydantic>=2`、`sqlalchemy>=2`、`asyncpg`、`alembic`、`redis`、`boto3`、`python-dotenv`、`openai`。
- dev 依赖：`pytest`、`pytest-asyncio`、`testcontainers`、`ruff`、`mypy`、`aiosqlite`。其中 `aiosqlite` 是 HANDBOOK 清单外的小幅补充：为任务 4 的内存 SQLite 夹具提供异步驱动，使 `pytest` 与 CI 不依赖 Docker（HANDBOOK 允许「内存/容器数据库」两种夹具，取内存路径）。
- `[tool.ruff.lint]`：`target-version = "py312"`；`ignore = ["EXE002"]`（可执行位无 shebang 属文件权限卫生，非代码质量）；`[tool.ruff.lint.per-file-ignores]` 对既有 stub 逐文件豁免：
  - `agent_runtime/guardrails/rails.py` = `["F401"]`
  - `connectors/base.py` = `["F401"]`
  - `domains/conversations/models.py` = `["F401"]`
  - `domains/outreach/models.py` = `["F401"]`
  - `domains/outreach/schemas.py` = `["F401"]`
  - `domains/sourcing/service.py` = `["F401", "I001"]`
  - `tool_gateway/pipeline.py` = `["F401"]`
  - `workflows/engine/runner.py` = `["F401"]`
  - `shared/events/bus.py` = `["PYI013", "PIE790"]`
  - `scripts/check_boundaries.py` = `["FURB167"]`
  - 逐文件豁免比全局关 F401 精确：不隐藏其它文件的真实死导入。
- `[tool.mypy]`：`python_version = "3.12"`；`[[tool.mypy.overrides]]` 对 `module = "shared.events.bus"` 设 `disable_error_code = ["misc"]`，注释写明「既有骨架类型问题，不改动骨架，基线豁免」。
- `[tool.pytest.ini_options]`：`testpaths = ["tests"]`、`asyncio_mode = "auto"`、`markers = ["db: 需 Docker 后端的 Postgres 容器测试"]`。

**失败预期 / 测试**
- 本任务前：`python -m pip install -e ".[dev]"` 因无 `pyproject.toml` 而失败（无包可安装）。
- 不加本配置时 `ruff check .` 报 243 处；加入配置后应为 0。
- 不加 override 时 `mypy domains shared tool_gateway` 报 `shared/events/bus.py:28`；加 override 后应 clean。

**验收命令**
```bash
conda run -n tradeos-py312 python -m pip install -e ".[dev]"
conda run -n tradeos-py312 python -c "import fastapi, sqlalchemy, pydantic, alembic, redis, boto3, dotenv, openai"
conda run -n tradeos-py312 ruff check .
conda run -n tradeos-py312 mypy domains shared tool_gateway
python3 scripts/check_boundaries.py
```
预期：安装成功、导入成功、ruff 0 违规、mypy 无错、check_boundaries 全绿。本任务未改动任何 `.py`，`--skeleton` 仍应通过。

**commit**：`chore: add pyproject.toml with lint/type/test configuration`

**push**：`git push`（本任务单独推送）

---

### 任务 2：Alembic 迁移基建 + 空基线迁移

**文件**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/0001_baseline_empty.py`

**实现**
- `alembic.ini`：`script_location = migrations`；数据库 URL 不写死在 ini，由 `env.py` 从环境读取。
- `migrations/env.py`：异步模板；经 `python-dotenv` 加载根 `.env`，读取 `DATABASE_URL`（`postgresql+asyncpg://...`），用 SQLAlchemy async engine 执行迁移；URL 缺失时给出明确报错并提示从 `infra/.env.example` 复制 `.env`，不得硬编码机器路径或本地凭证。
- `migrations/versions/0001_baseline_empty.py`：`upgrade()` 与 `downgrade()` 均为空实现，docstring 写明作用——**空基线用于锚定版本链起点**；领域表不在此处创建，每张表随所属切片（HANDBOOK 切片 2 起）作为独立任务落地。这是有意的边界划分：工程基建不提前引入业务 schema。

**失败预期 / 测试**
- 本任务前：`alembic` 命令不存在（依赖由任务 1 安装）。
- `alembic upgrade head` 在 postgres 未启动时报连接错误；postgres 启动后应成功并落在 0001。
- 注意：加入真实迁移代码 `migrations/env.py` 后，`python3 scripts/check_boundaries.py --skeleton` 会在该文件报 stub-purity——这是 HANDBOOK 明示的过渡点（开始实现后 `--skeleton` 从 CI 移除）；常规 `python3 scripts/check_boundaries.py` 不受影响，必须全绿。

**验收命令**
```bash
docker compose -f infra/docker-compose.yml up -d
conda run -n tradeos-py312 alembic upgrade head
conda run -n tradeos-py312 alembic current     # 预期 0001
conda run -n tradeos-py312 alembic history     # 预期列出 0001_baseline_empty
python3 scripts/check_boundaries.py
```

**commit**：`chore(db): add alembic migration scaffolding with empty baseline`

**push**：`git push`

---

### 任务 3：infra/db 租户过滤基类

**文件**
- Create: `infra/__init__.py`
- Create: `infra/db/__init__.py`
- Create: `infra/db/base.py`
- Create: `tests/unit/test_infra_db.py`

**实现**（对应 HANDBOOK「最关键的一件」：租户过滤在基类统一注入，不靠每个查询点自觉）
- `infra/db/base.py` 定义 `TenantScopedRepository`：
  - 构造时绑定 `tenant_id: str`。Phase 1 单租户，以 UUID 文本承载租户维度即满足硬边界 8 的隔离要求；不依赖尚未实现的 shared 类型。
  - `scoped_query(session, model)`：返回已自动注入 `WHERE tenant_id = :tenant_id` 的 `select`，调用方不得绕过该过滤。
  - `unsafe_cross_tenant_query(session, model, audit_reason)`：运维专用后门。缺 `audit_reason` 直接抛错；提供理由时先写结构化审计日志（logger 名 `infra.db.audit`，记录 tenant_id 与 reason）再返回未过滤查询。把后门做成显式且留痕。
  - 类型注解与 docstring 完整（中文写明要实现什么、边界、为什么）。
- `tests/unit/test_infra_db.py`：不依赖数据库，用 SQLAlchemy 编译产物断言：
  1. `scoped_query` 生成 SQL 的 WHERE 子句含 `tenant_id`，且绑定值等于本仓库 tenant_id；
  2. `unsafe_cross_tenant_query` 缺 `audit_reason` 抛错；
  3. 提供 `audit_reason` 时返回未过滤查询，且用 caplog 断言审计日志记录了 tenant_id 与 reason。

**失败预期 / 测试**
- 本任务前：`pytest tests/unit/test_infra_db.py` 报文件不存在。
- 实现中：任何绕过基类租户注入的路径被测试 1 拦住；后门不留痕被测试 3 拦住。
- `--skeleton` 会在 `infra/db/base.py` 报 stub-purity（真实基建代码，预期；过渡点已在任务 2 说明）。

**验收命令**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_infra_db.py -q
python3 scripts/check_boundaries.py
```
预期：测试全绿、check_boundaries 全绿。

**commit**：`feat(infra): add tenant-scoped repository base class`

**push**：`git push`

---

### 任务 4：tests/conftest.py 测试夹具

**文件**
- Create: `tests/conftest.py`
- Create: `tests/unit/test_conftest_smoke.py`

**实现**
- `tests/conftest.py` 提供内存数据库夹具（HANDBOOK「内存/容器数据库」取内存路径，驱动为任务 1 补充的 `aiosqlite`）：
  - `db_engine`：async engine，`:memory:` + StaticPool（跨连接共享）；
  - `db_session`：绑定引擎的 async session，测试后回滚（async fixture 显式用 `@pytest_asyncio.fixture`）；
  - `tenant_id`：Phase 1 单租户固定 ID 字符串。
  - 夹具内的测试模型为最小声明式行模型（仅 `id` / `tenant_id` / `name`，非业务模型）。
- `tests/unit/test_conftest_smoke.py`：
  1. 用 `db_session` 写入一行再读回，断言 round-trip 成立；
  2. 写入租户 A 与租户 B 各一行，按租户过滤查询各自只能读回本租户行（数据层隔离预演硬边界 8）。
- 目的：`pytest -q` 从此收集到真实通过的测试（不再以「no tests ran」退出码 5），且不依赖 Docker。

**失败预期 / 测试**
- 本任务前：`pytest -q` 报「no tests ran」（exit 5），`make check` 因此无法全绿。
- 实现中：若 fixture 未正确回滚，测试间数据互相污染，测试 2 会失败。

**验收命令**
```bash
conda run -n tradeos-py312 pytest tests/unit/test_conftest_smoke.py -q
conda run -n tradeos-py312 pytest -q
python3 scripts/check_boundaries.py
```
预期：两组测试全绿、`pytest -q` 整体通过（exit 0）、check_boundaries 全绿。

**commit**：`test: add pytest fixtures for database-backed tests`

**push**：`git push`

---

### 任务 5：Makefile 命令入口

**文件**
- Create: `Makefile`

**实现**（在 `tradeos-py312` 环境内执行，用裸命令名，不硬编码机器路径）
- `dev`：`docker compose -f infra/docker-compose.yml up -d`
- `migrate`：`alembic upgrade head`
- `test`：`pytest -q`
- `check`：`ruff check . && mypy domains shared tool_gateway && python3 scripts/check_boundaries.py && pytest -q`（HANDBOOK 规定的四件；`--skeleton` 已按过渡点移除）
- `check:skeleton`：`python3 scripts/check_boundaries.py --skeleton`（骨架阶段核对用）
- `clean`：清理 `__pycache__`、`.pytest_cache`、`.ruff_cache`、`.mypy_cache`
- 所有目标声明 `.PHONY`

**失败预期 / 测试**
- 本任务前：`make check` 因缺 Makefile 而失败；任务 1–4 未完成时 `make check` 的 pytest/mypy/ruff 环节会红。
- 完成后：`make check` 四件全绿，`make test` 全绿。

**验收命令**
```bash
make check
make test
make dev
```

**commit**：`chore: add Makefile command entrypoints`

**push**：`git push`

---

### 任务 6：CI 工作流

**文件**
- Create: `.github/workflows/ci.yml`

**实现**
- 触发：push（含本分支）与 pull_request。
- job：`ubuntu-latest` + `setup-python` 3.12；`pip install -e ".[dev]"` 后依次运行：
  - `ruff check .`
  - `mypy domains shared tool_gateway`
  - `python3 scripts/check_boundaries.py`（不含 `--skeleton`，理由同任务 5）
  - `pytest -q`
- 本地无法执行 GitHub Actions 语义校验；本机验收覆盖工作流内容的真实行为。

**失败预期 / 测试**
- 本任务前：无 `.github/workflows/ci.yml`。
- 若四步命令中任何一步本地失败（即任务 1–5 未全绿），工作流在 GitHub 上必然失败——因此先保证本地全绿。

**验收命令**
```bash
cat .github/workflows/ci.yml   # 核对四个 run 步骤与上表命令逐字一致
make check                     # 四件在本地全绿
```

**commit**：`ci: add boundary/lint/type/test workflow`

**push**：`git push`

---

## 验收汇总（全部任务完成后）

```bash
make check          # ruff / mypy / check_boundaries / pytest 四件全绿
make test           # pytest 全绿
make dev            # postgres/redis/minio 启动
make migrate        # alembic upgrade head 成功
```

## 风险与说明

- `--skeleton` 的 stub-purity 从任务 2 起会在真实基建文件（`migrations/env.py`、`infra/db/base.py`）上报——这是 HANDBOOK 明示的过渡点（开始实现后从 CI 移除），常规 `check_boundaries.py` 不受影响，CI 采用常规检查。
- `aiosqlite` 是 HANDBOOK 依赖清单外的小幅补充，换取 `make check` 与 CI 不依赖 Docker。
- 基线 ruff/mypy 豁免（见「已知基线」）是配置层收敛，未改任何骨架文件。
