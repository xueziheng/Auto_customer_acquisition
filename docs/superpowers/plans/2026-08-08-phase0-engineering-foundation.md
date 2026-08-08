# Phase 0 · 工程基建实施计划

> **给执行者的说明**：本计划由零号任务（监督者 + 实现者）编写并已按监督验收意见修订，覆盖 HANDBOOK「二、第 0 步：补齐工程基建」的全部交付。按 superpowers 计划约定逐任务打勾。**范围硬性限定在第 0 步工程基建**：除任务 0 这一处经监督批准的 shared 类型标注修复外，不实现 shared 契约、不实现任何业务域、不改动既有业务骨架、不建业务迁移表。
>
> 每个任务独立可 review，格式统一为：文件 / 实现 / 失败预期与测试 / 验收命令 / commit / push。每任务提交前必须 `python3 scripts/check_boundaries.py` 全绿，每任务单独 commit、单独 push。

**目标**：让 `make dev` 起数据库、`make test` 跑通测试、`make check` 四件全绿（ruff / mypy / check_boundaries / pytest），为后续切片实现提供可运行的工程地基。

**执行环境**：`tradeos-py312`（conda，Python 3.12.13，零号任务已创建）。所有 Python 命令在该环境中执行；`python3 scripts/check_boundaries.py` 用系统 python3（结构检查脚本为纯 stdlib，与 Python 版本无关）。零号任务已在本环境预装 `ruff`、`mypy` 用于基线核验；任务 1 起由 pyproject 的 dev 依赖正式接管。

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
- **tenant_id 一律用强类型 `TenantId`**（`shared.schemas.identifiers.TenantId`，`NewType(str)`，运行时零开销）。禁止在 `infra/db`、测试夹具或任何新代码里把租户维度降级成裸 `str`——`infra → shared` 是向下依赖，符合硬边界 9 的依赖方向。同理，操作者/服务主体用 `UserId`。
- **依赖方向**：新增代码不得引入反向导入、跨域导入或 SDK 越界（硬边界 9，`check_boundaries.py` 机检）。
- **每任务提交前**：`python3 scripts/check_boundaries.py` 必须全绿。
- **每任务单独 push**：一个任务一个 commit、一次 push，便于逐项 review。本计划共 7 个任务 → 7 个 commit、7 次 push。
- **本机准备不提交**：不提交任何机器路径、凭证或 `.env`；`.env` 由本机从 `infra/.env.example` 复制填充且永不提交。
- **不碰骨架**：不改 AGENTS.md 硬边界；除任务 0 明确列出的单行改动外，不改动既有业务骨架文件内容。
- **不新增全局豁免**：Phase 0 不新增全局 F401 或 mypy `misc` 类豁免；ruff 只允许逐文件豁免，mypy 不允许任何 `disable_error_code`。

### 已知基线

- **ruff**：既有骨架报 243 处违规——230 处 EXE002（可执行位无 shebang，文件权限卫生）+ 13 处（骨架 stub 的预留导入 F401、`...` 占位 PIE790/PYI013、导入排序 I001、脚本别名 FURB167）。任务 1 通过 pyproject 配置忽略 EXE002 并按文件豁免其余 13 处；**逐文件豁免均带退出条件**（见任务 1），不改任何骨架文件内容。
- **mypy**：既有骨架在 `shared/events/bus.py:28` 有 1 处真实类型错误（Protocol 类型变量不变性）。**不用配置涂绿**——由前置任务 0 做最小类型标注修复使其真正通过（监督已批准这一处 shared 改动）。

---

## 前置条件（本机准备，不提交）

- [x] conda 环境 `tradeos-py312`（Python 3.12.13）已创建并核验（零号任务完成）；`ruff`、`mypy` 已预装（基线核验用）。
- [ ] `docker compose -f infra/docker-compose.yml up -d` 可启动 postgres/redis/minio（任务 2 验收用到）。
- [ ] 根目录 `.env`：`cp infra/.env.example .env` 后填本地 `DATABASE_URL`（如 `postgresql+asyncpg://tradeos:tradeos_local@localhost:5432/tradeos`）。`.env` 永不提交。

---

## 任务列表

### 任务 0：修正 EventHandler 泛型方差（shared 静态契约修复，监督批准的唯一一处骨架改动）

**文件**
- Modify: `shared/events/bus.py`（仅第 24 行）
- Create: `docs/adr/0007-event-handler-contravariance.md`（理由见下）
- Modify: 本计划文件（记录任务 0 的 ADR 决策）

**实现**
- 把 `E = TypeVar("E", bound=DomainEvent)` 改为 `E = TypeVar("E", bound=DomainEvent, contravariant=True)`。
- 理由：`E` 在本文件只出现在参数位——`EventHandler.handle(self, event: E)`（第 38 行）与 `EventBus.subscribe(..., handler: EventHandler[E])`（第 68 行）——这是逆变位置，不变 TypeVar 在 Protocol 中触发 mypy `misc` 错误。改为 contravariant 让静态检查正确反映既有的运行期语义：处理器接受基类事件即可用于派生事件。仅类型标注层面的修正，不改变任何运行时行为、事件字段、Provenance 或 Money 语义。
- 已实测：改动前 `mypy domains shared tool_gateway` 报 `shared/events/bus.py:28` 1 处错误；改动后 `Success: no issues found in 137 source files`；`python3 scripts/check_boundaries.py --skeleton` 保持全绿（本改动不在任何函数体，不影响 stub 纯度）。全库仅 bus.py 自身使用 `EventHandler`，改动无外溢。

**ADR 判断（规则依据）**
- **需要 ADR。** `shared/AGENTS.md §改动约束`：`shared/` 的每一个改动都是**公共 API 变更**。`EventHandler` 是跨域订阅使用的公共契约（域 events.py 经 `shared.events.catalog` 订阅，装配依赖其泛型签名），泛型方差的任何变化都属于公共 API 变更，因此必须留 ADR。监督已据此收紧判断（原「不需要 ADR」的结论作废）。
- 本任务随代码一并创建 `docs/adr/0007-event-handler-contravariance.md`，按模板记录背景、决策、理由、放弃的选项与后果。

**失败命令 / 通过命令**
```bash
# 失败（改动前）：
conda run -n tradeos-py312 mypy domains shared tool_gateway
#   预期输出：shared/events/bus.py:28: error: Invariant type variable "E" ... [misc]
# 通过（改动后）：
conda run -n tradeos-py312 mypy domains shared tool_gateway
#   预期输出：Success: no issues found in 137 source files
python3 scripts/check_boundaries.py --skeleton     # 仍全绿
```

**注意**：本任务不删除 bus.py 的 ruff 豁免——该文件仍是 stub，`...` 占位保留（PYI013/PIE790 豁免按退出条件在首次实现 bus.py 时移除，见任务 1）。

**commit**：`fix(shared): make EventHandler TypeVar contravariant`

**push**：`git push`（本任务单独推送）

**完成记录（任务 0.1，监督插入）**：监督验收发现 `target-version = "py312"` 下 Ruff PLC0105 提示「逆变 TypeVar 命名 `E` 未反映方差」。据此把 `shared/events/bus.py` 中的逆变 TypeVar 统一重命名为 `E_contra`（全文件 4 处，无语义变化）；commit `3232e70`，message `fix(shared): align contravariant TypeVar naming`，已推送。ADR 0007 决策代码片段已同步为 `E_contra`。

---

### 任务 1：pyproject.toml 工程配置

**文件**
- Create: `pyproject.toml`

**实现**
- 构建：setuptools，flat layout；`[tool.setuptools.packages.find]` 纳入 `shared*`、`domains*`、`tool_gateway*`、`notification_gateway*`、`artifact_store*`、`connectors*`、`agent_runtime*`、`workflows*`、`apps*`、`infra*`，排除 `tests` 与 `scripts`。
- `[project]`：`requires-python = ">=3.12"`；运行时依赖按 HANDBOOK：`fastapi`、`uvicorn`、`pydantic>=2`、`sqlalchemy>=2`、`asyncpg`、`alembic`、`redis`、`boto3`、`python-dotenv`、`openai`。
- dev 依赖：`pytest`、`pytest-asyncio`、`testcontainers`、`ruff`、`mypy`、`aiosqlite`。其中 `aiosqlite` 是 HANDBOOK 清单外的小幅补充：为任务 4 的内存 SQLite 夹具提供异步驱动，使 `pytest` 与 CI 不依赖 Docker（HANDBOOK 允许「内存/容器数据库」两种夹具，取内存路径）。
- `[tool.ruff.lint]`：`target-version = "py312"`；`ignore = ["EXE002"]`（可执行位无 shebang 属文件权限卫生，非代码质量）；`[tool.ruff.lint.per-file-ignores]` 对既有 stub 逐文件豁免，**每条豁免带退出条件（见下）**：
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
- **豁免退出条件**：除 `scripts/check_boundaries.py` 外，以上均为骨架 stub 豁免。**后续任务首次实现对应文件（其函数体不再是 NotImplementedError/`...`）时，必须在同一 commit 删除该文件的豁免**；不得提前删（删早了会报错），也不得拖到实现之后再删。`scripts/check_boundaries.py` 的 FURB167 不是骨架豁免——该脚本是既有真实脚本，不是 stub，无「实现」任务，故不适用退出条件规则；属一次性风格容忍（`re.M` 别名），如需处理应在专门改动该脚本的提交中一并完成。
- **Phase 0 不新增全局豁免**：不引入全局 `F401`，不引入任何 mypy `disable_error_code`（任务 0 已把唯一一处类型错误修掉）。
- `[tool.mypy]`：`python_version = "3.12"`。**无任何 `overrides` / `disable_error_code`**。
- `[tool.pytest.ini_options]`：`testpaths = ["tests"]`、`asyncio_mode = "auto"`、`markers = ["db: 需 Docker 后端的 Postgres 容器测试"]`。

**失败预期 / 测试**
- 本任务前：`python -m pip install -e ".[dev]"` 因无 `pyproject.toml` 而失败（无包可安装）。
- 不加本配置时 `ruff check .` 报 243 处；加入配置后应为 0（逐文件豁免精确覆盖 13 处，EXE002 全局忽略覆盖 230 处）。
- mypy：任务 0 已修复唯一错误，本任务不配置任何豁免，`mypy domains shared tool_gateway` 应直接 clean。

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
- `infra/db/base.py` 定义 `TenantScopedRepository`，强类型租户/身份一律用 `shared.schemas.identifiers` 的 `NewType`：
  - `from shared.schemas.identifiers import TenantId, UserId`（`infra → shared` 为向下依赖，符合硬边界 9）。
  - 构造时绑定 `tenant_id: TenantId`（`TenantId = NewType("TenantId", str)`，运行时零开销，静态可区分）。
  - `scoped_query(session, model)`：返回已自动注入 `WHERE tenant_id = :tenant_id` 的 `select`，调用方不得绕过该过滤。
  - `unsafe_cross_tenant_query(session, model, actor_id: UserId, audit_reason: str)`：运维专用后门。**`actor_id` 与 `audit_reason` 都必须非空**（`strip()` 后仍为空即抛错）；两者有效时先写结构化审计日志（logger 名 `infra.db.audit`，**同时记录 `actor_id`、构造绑定的 `tenant_id`、`audit_reason`**）再返回未过滤查询。把后门做成显式、有操作者留痕、有理由留痕。
  - 类型注解与 docstring 完整（中文写明要实现什么、边界、为什么）。
- `tests/unit/test_infra_db.py`：不依赖数据库，用 SQLAlchemy 编译产物断言：
  1. `scoped_query` 生成 SQL 的 WHERE 子句含 `tenant_id`，且绑定值等于本仓库构造时传入的 `TenantId`；
  2. `unsafe_cross_tenant_query` 拒绝空 `actor_id`（`UserId("")`）抛错；
  3. 拒绝空 `audit_reason`（`""`）抛错；
  4. `actor_id` 与 `audit_reason` 均有效时返回未过滤查询，且用 caplog 断言结构化审计日志同时记录了 `actor_id`、绑定的 `tenant_id`、`audit_reason`。

**失败预期 / 测试**
- 本任务前：`pytest tests/unit/test_infra_db.py` 报文件不存在。
- 实现中：任何绕过基类租户注入的路径被测试 1 拦住；后门缺操作者或理由被测试 2/3 拦住；后门不留痕被测试 4 拦住。
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
  - `tenant_id`：返回 `TenantId` 类型固定值（Phase 1 单租户），如 `TenantId("tenant_phase1")`——不得用裸 str。
  - 夹具内的测试模型为最小声明式行模型（`id` / `tenant_id` / `name`；`tenant_id` 列 `Mapped[str]`，写入值用 `TenantId(...)`，运行时即 str、静态可区分），非业务模型。
- `tests/unit/test_conftest_smoke.py`：
  1. 用 `db_session` 写入一行再读回，断言 round-trip 成立；
  2. 写入租户 A 与租户 B（`TenantId("tenant_a")` / `TenantId("tenant_b")`）各一行，按租户过滤查询各自只能读回本租户行（数据层隔离预演硬边界 8）。
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
- 本任务前：`make check` 因缺 Makefile 而失败；任务 0–4 未完成时 `make check` 的 pytest/mypy/ruff 环节会红。
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
- 若四步命令中任何一步本地失败（即任务 0–5 未全绿），工作流在 GitHub 上必然失败——因此先保证本地全绿。

**验收命令**
```bash
cat .github/workflows/ci.yml   # 核对四个 run 步骤与上表命令逐字一致
make check                     # 四件在本地全绿
```

**commit**：`ci: add boundary/lint/type/test workflow`

**push**：`git push`

---

## 验收汇总（全部 7 个任务完成后）

```bash
make check          # ruff / mypy / check_boundaries / pytest 四件全绿
make test           # pytest 全绿
make dev            # postgres/redis/minio 启动
make migrate        # alembic upgrade head 成功
```

**提交/推送计数**：任务 0–6 共 7 个独立 commit、7 次独立 push（每个任务提交前 `python3 scripts/check_boundaries.py` 全绿）。

## 风险与说明

- `--skeleton` 的 stub-purity 从任务 2 起会在真实基建文件（`migrations/env.py`、`infra/db/base.py`）上报——这是 HANDBOOK 明示的过渡点（开始实现后从 CI 移除），常规 `check_boundaries.py` 不受影响，CI 采用常规检查。任务 0 与任务 1 不改任何函数体，`--skeleton` 仍全绿。
- `aiosqlite` 是 HANDBOOK 依赖清单外的小幅补充，换取 `make check` 与 CI 不依赖 Docker。
- 任务 0 是本计划唯一触碰 shared 骨架的改动（监督已批准），并随代码留 ADR `docs/adr/0007`（shared 每个改动都是公共 API 变更，见 `shared/AGENTS.md §改动约束`）。
- 基线 ruff/mypy 处置均为配置/最小标注收敛，未改任何业务逻辑文件内容。
