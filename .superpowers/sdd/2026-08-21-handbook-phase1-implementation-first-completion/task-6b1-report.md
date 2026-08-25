# Task 6B1 实施报告

日期：2026-08-25
初次实现提交：`d87a3e8f08965b46da779fa41a5e06df9f9e9a64`
审查修复提交：`804a72b99209a1970b88ece72350816f6592a90c`

## 结论

Task 6B1 的 Account Discovery 与 Demand Discovery 验收已在受控本地假数据、
测试替身及 disposable PostgreSQL 上完成。没有调用真实 Hunter、Web、Gmail，
没有客户发送、部署或 push，也不据此声称 Phase 1 已投入运营。

## 既有覆盖盘点

没有为了匹配历史文件名而复制等价测试；以下行为复用既有覆盖：

- canonical website domain 去重及 tenant guard：
  `tests/unit/test_prospecting_service.py`、
  `tests/integration/test_prospecting_repositories.py`。
- 联系人 legal basis、四种验证状态、隐私删除与 recollection suppression、
  verified-only 入组前置：prospecting domain/repository 与 contact gateway 既有测试。
- enrollment 的 tenant 隔离、重放与幂等：
  `tests/unit/test_outreach_enrollment_service.py`、
  `tests/integration/test_outreach_enrollment_lifecycle.py`。
- Demand signal/hypothesis 的租户隔离、visible evidence、证据等级、事实/推断分离：
  `tests/integration/test_demand_signals.py`、
  `tests/integration/test_need_hypotheses.py`、
  `tests/unit/test_need_hypothesis_models.py`、`tests/unit/test_evidence.py`。

新增聚合测试只补 agent、workflow、Web gateway 和数据库迁移边界上原来没有直接证明的
行为，不新增等价的 `test_account_discovery_postgres.py` 或
`test_demand_discovery_postgres.py`。

## 找到并修复的真实缺口

1. Demand workflow 已生成 snapshot artifact，但在转换为
   `SignalCaptureRequest` 时丢失，PostgreSQL 只保存 URL、时间和 hash。
   已将 `snapshot_artifact_ref` 贯通 agent ChangeSet、workflow、domain schema/model、
   repository、ORM 和 Alembic `0034`；网页信号必须持有完整四元组，非网页信号禁止
   携带 snapshot ref。
2. `AccountDiscoveryAgent` 会把 hypothesis reasoning/evidence 中的联系人姓名、
   邮箱、电话原样交给模型。现由确定性代码对所有自由文本和 URL 做保守检查；任何
   疑似联系人姓名、邮箱、电话或 credential marker 都在模型调用前 fail-closed，
   不再依赖 prompt 约束或局部脱敏。
3. Demand numeric probability 护栏未识别中文后缀形式 `82% 概率`，现已拒绝。
4. PostgreSQL CHECK 若只依赖正则，`NULL` 会以三值逻辑绕过。0034/ORM 约束现显式要求
   web `snapshot_artifact_ref IS NOT NULL`。

## 0034 backfill 行为

- 0 匹配：升级抛 PostgreSQL `23514` 并保持 0033，绝不生成虚假 artifact 引用。
- 1 匹配：按同 tenant、`web_snapshot` kind、相同 content hash 恢复原 artifact ID；
  `0034 → 0033 → 0034` roundtrip 已验证。
- 多匹配：正常生产路径先由
  `uq_raw_artifacts_tenant_kind_hash(tenant_id, kind, content_hash)` 拒绝；0034 还在
  UPDATE 前显式计算每条 web signal 的匹配数。即使模拟异常历史数据、临时移除唯一
  约束并形成两个匹配，升级也以 `23514` fail-closed，绝不让 PostgreSQL
  `UPDATE ... FROM` 任意选择，因此不会降低证据质量。

## TDD 证据

见证的 RED：

1. `pytest tests/integration/test_demand_signals.py::test_capture_roundtrip_persists_all_columns -q`
   因 `SignalCaptureRequest` 不接受 `snapshot_artifact_ref` 失败。
2. `pytest tests/unit/agent_runtime/test_account_discovery_agent.py::test_contact_pii_is_redacted_before_account_model_input -q`
   捕获的模型输入仍含合成邮箱和电话而失败。
3. `pytest tests/unit/agent_runtime/test_demand_intelligence_agent.py::test_numeric_confidence_in_inference_is_rejected -q`
   `82% 概率` 未被护栏拒绝而失败。
4. `pytest tests/integration/test_migrations.py::test_demand_signals_web_evidence_rejects_incomplete_tuple -q`
   PostgreSQL 接受 `snapshot_artifact_ref=NULL` 的 web 行而失败。

每个生产修复均先有上述行为 RED，再做最小修复并见证 GREEN。

## 变更文件

- Account/Demand agent：
  `agent_runtime/account_discovery/agent.py`、
  `agent_runtime/demand_intelligence/agent.py`。
- Demand domain/persistence/workflow：
  `domains/demand/{errors,models,schemas,service,service_impl}.py`、
  `infra/db/repositories/demand.py`、`infra/db/tables.py`、
  `workflows/demand_discovery/steps.py`。
- Migration/operation head：
  `migrations/versions/0034_demand_signal_snapshot_artifacts.py`、
  `docs/operations/hunter-provider-readiness.md` 及相关 head/roundtrip 测试。
- Acceptance tests：
  `tests/unit/agent_runtime/`、`tests/unit/workflows/`、
  `tests/unit/test_web_search_discovery.py`，以及受影响的 demand、migration、
  provider-readiness 既有测试。

## GREEN 与门禁

- 新增 Account/Demand agent、workflow、Web boundary：`23 passed`。
- 受影响单元测试选择集：`210 passed`。
- 相关 PostgreSQL domain/gateway 集成：`58 passed`。
- 迁移选择集（补充三态前）：`8 passed`。
- 0034 单/零匹配专项（同时断言重复匹配被唯一约束拒绝）：`2 passed`。
- 最终受影响聚合回归：`277 passed in 15.81s`。
- Ruff（全部 changed Python）：`All checks passed!`。
- configured mypy + affected agent modules：`359 source files`，无问题。
- `python scripts/check_boundaries.py`：全部七项通过。
- `python scripts/scan_sensitive.py`：exit 0，无输出。
- `python scripts/run_alembic.py heads`：`0034 (head)`。
- `git diff --check`：exit 0。

## 剩余风险

代码验收没有已知失败或 flaky。唯一需要在真实环境迁移前处理的操作风险是：如果已有
web demand signal 无法按 tenant/kind/hash 关联到 `raw_artifacts`，0034 会有意
fail-closed。上线前应先做历史数据审计和可追溯 backfill；不得绕过约束或合成 artifact
引用。

预存未跟踪的 `apps/web/node_modules` 符号链接未纳入提交。

## Code Review Fix Round 1/5

独立复审提出四项 Important finding，本轮逐项修复，没有扩大到 6B2/6B3/6C：

1. **联系人姓名仍可进入模型。** 输入投影改为保守、确定性的 fail-closed：邮箱、
   电话、联系人/动作上下文、英文单名/多词姓名、角色型姓名，以及典型中文姓名/发言
   结构都会在模型调用前拒绝。企业名只在具有明确组织后缀时作为组织短语放行；无法
   可靠区分的文本宁可不调用模型。
2. **模型输出字符串可绕过 ChangeSet 护栏。** 现在递归检查模型 JSON 中所有字符串，
   拒绝姓名、邮箱、电话、凭证 marker、数值概率和动作指令；企业名必须逐字出现在
   安全证据摘要中。构造 ChangeSet 后还复用中央 `guard_phase1_change_set`。真实
   `FindCompanyDetailsStep` 回归证明危险输出只产生
   `{"company_resolution": "no_evidence"}`，不会进入 workflow context/ledger patch。
3. **概率变体覆盖不足。** `NoProbabilityOutputRail` 提取为中央
   `contains_numeric_probability`，统一 NFKC/casefold；现在拒绝 `概率为82%`、
   `可能性为 0.82`、`概率为８２％` 和既有反向表达，不放宽 evidence-tier 规则。
4. **snapshot artifact 只做形状检查。** demand 域新增最小 repository Protocol，
   由同一 UoW/session 验证 artifact 同租户存在、kind=`web_snapshot`、content hash
   精确匹配；ORM/0034 添加 `(tenant_id, snapshot_artifact_ref)` 复合外键到
   `raw_artifacts`，数据库直接拒绝缺失或跨租户引用。web hash 收紧为 64 位小写
   SHA-256。

### 本轮见证 RED

- Account 输入/输出与持久化路径：首次选择集 `8 failed, 2 passed`；补强姓名结构后
  分别见证 `3 failed, 1 passed` 和 `2 failed, 4 passed`，失败均为模型仍被调用或
  危险 ChangeSet 仍被生成。
- Probability 四种表达：`3 failed, 1 passed`；仅既有反向形式原本能拒绝。
- Artifact service 的 missing/cross-tenant/wrong-kind/wrong-hash：`4 failed`；DB
  tenant FK：`1 failed`，均为非法状态仍被接受。
- 0034 异常多匹配：`1 failed`，确认旧 `UPDATE ... FROM` 会任意选一个并成功升级。

### 本轮 GREEN 与最终门禁

- Account agent/workflow：`16 passed`；最后增加英文单名和独立中文姓名后，当前提交
  的最终受影响选择集为 `21 passed`。
- Probability 中央 rail + demand agent：`6 passed`。
- Artifact 四状态 + DB tenant FK：`5 passed`。
- 0034 backfill 0/1/多匹配专项：`3 passed`。
- 受影响单元/工作流聚合：`54 passed`。
- Demand signal/need + 迁移选择聚合：`51 passed`。
- 整份迁移回归：`55 passed in 45.12s`。
- 全库回归：`3883 passed in 462.43s`；随后仅增加更保守的姓名检测与对应测试，
  其全部调用点（Account agent/workflow）及修改的 outbox 断言再次 `21 passed`。
- Ruff（最终 changed Python）：`All checks passed!`。
- Mypy（11 个受影响 production source）：无问题；最终姓名检测单文件复跑亦无问题。
- `scripts/check_boundaries.py`：七项全部通过。
- `scripts/scan_sensitive.py`：exit 0、无输出。
- `scripts/run_alembic.py heads`：唯一 `0034 (head)`。
- `git diff --check`：exit 0。

### 兼容性与剩余风险

- 旧调用方使用非 64 位小写 SHA-256 的 web `page_hash` 现在会在持久化前失败；这是
  为了让内容寻址与 RawArtifact 契约一致的有意收紧，不做静默转换。
- 0034 对历史 web signal 的 0 匹配或多匹配统一 fail-closed；上线前必须审计并以真实
  不可变快照修复历史数据，不得合成引用。
- 本轮没有真实网络、Hunter/Gmail key、客户发送、部署或 push，也不据此作运营完成
  声明。预存未跟踪 `apps/web/node_modules` 仍未纳入提交。
