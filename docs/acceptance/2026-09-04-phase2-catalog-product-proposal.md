# Phase2 Catalog Product Proposal 验收记录

## 结论与范围

Catalog Product Proposal 子项目的工程实现、受控功能链和全仓门禁已经通过。当前结论只表示：
在隔离本地租户内，经独立人工审批的显式策略可以把符合确定性规则的 Need Cluster 生成内部候选提案，
第二次独立审批最多创建一个 `queued` 培养 Case。它不表示整个 Phase2 完成，也不表示正式 Product、
供应确认、可报价价格、真实市场需求或生产启用。

- 实施基线：`2cb751ee71bee796754ce8e1e65bfecc8978b318`。
- 功能与最终验证 HEAD：`50fe7abbe9ee81d066788eb81c9cf10fb9e3a11a`（不含本验收文档提交）。
- 工作树：`phase2-catalog-product-proposal` 隔离 worktree。
- 受控核心：真实 PostgreSQL migrations/SQLAlchemy repository、transactional Outbox、Workflow
  engine、scheduler advisory lock cycle/runtime 重建、FastAPI、Vite 与 Chromium。
- 只有 Account、Validated Need、Need Cluster、合格 Provenance 和 product Employee 是隔离合成输入；Catalog
  policy/evaluation/proposal/cultivation 与 Approval 业务行都经生产端口创建和推进。
- 本轮没有 merge、push、deploy、生产策略激活或真实外部行为。

```yaml
controlled_postgres_api_scheduler_browser: passed
full_repository_gate: passed
real_external_provider_calls: not_run
real_supplier_contact: not_run
real_customer_contact: not_run
real_quote_or_price_commitment: not_run
production_policy_activation: not_run
```

Prompt、model 和 eval 数据集未改变；model/eval 工作记为 `not_applicable`。

## RED → GREEN 与复审修正

初始 E2E 在完整受控链尚未闭合时按预期失败，形成真实 RED：

```text
TRADEOS_REQUIRE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_catalog_product_proposal_controlled.py -q -rs
1 failed in 15.24s
```

实现后又通过独立复审发现并修正了三类会造成错误结论的问题：

1. E2E 曾用非生产格式的 Need/Account ID，可能形成假绿；现改用生产 canonical `need_`/`acc_` ID，
   并显式拒绝旧 `vnd_`/`acct_` 格式。
2. UI 曾预填客户数门槛 `3`，与“业务门槛无默认值”冲突；现初始值和重置值均为空，提交时要求用户显式输入正整数。
3. 390px 提案卡曾裁切状态徽标和负责人 ID；现已修复布局，并增加提案卡、状态、负责人及局部
   `scrollWidth <= clientWidth` 元素级断言。断言只容忍 0.5 CSS px 的子像素舍入，不放过真实溢出。

为闭合全仓门禁，还隔离了 recurring demand 与 Slice4 checkpoint 的测试数据库状态，更新 migration head
至 `0058`，并让 Slice4 Browser E2E 经公开 scheduler worker 与单副本锁运行；没有削弱迁移保护、锁或
no-handler 失败关闭语义。

最终受控 E2E：`1 passed in 19.96s`。它证明：

1. 三个独立 Account 各绑定一个 Validated Need，品类精确为 `three-wheelers`，国家事实为 `KE`，
   country/category Evidence 都有可路由 conversation ID 和已确认 Provenance。这些是合成验收事实，
   不是 Kenya 市场结论。
2. 无活动策略时产生 0 policy/evaluation/proposal/case；门槛只能由 product 员工显式提交，再由不同 boss
   通过中央 Approval 精确深链审批。
3. 通过评估精确绑定 3 个 Account/Need 和 1 个 `KE`。未配置的复购、数量/单位、国家数和统一单位门槛
   不会被系统虚构为已知事实。
4. 提案批准只创建 1 个 `queued` 培养 Case。重复事件、重复决定和 runtime 重启后仍只有一份对应业务事实。
5. canonical 事实变化使旧快照转为 `stale`，不会因旧批准创建第二个 Case；恢复后以新 facts hash 创建新快照。
6. 错租户访问稳定拒绝；Browser 决策请求体不接受 tenant、actor、facts hash、Provenance、owner 或 approver。

## 零外部效果证据

受控链运行前后均核对真实表和传输计数：

| 事实/能力 | 前 | 后 |
| --- | ---: | ---: |
| formal Product | 0 | 0 |
| Supplier | 0 | 0 |
| Prospect Contact / Contact Point | 0 | 0 |
| Sourcing Search Execution | 0 | 0 |
| Outreach Message Attempt | 0 | 0 |
| Quote | 0 | 0 |
| durable Tool Gateway Call | 0 | 0 |
| Tavily usage/search | 0 / 0 | 0 / 0 |
| public-page validate/fetch | 0 / 0 | 0 / 0 |
| model | 0 | 0 |
| external network | 0 | 0 |

没有造数价格、供应商、联系人、搜索、发送或 Product promotion 来让测试通过，也没有读取、打印或复制
任何 secret value。

## Browser 与截图人工检查

Browser 使用 fresh Chromium context 和真实 Vite/FastAPI 请求，完整穿越 Product Center → 精确
Approval Center → Product Center。页面级和关键卡片元素级均检查横向溢出；错误 overlay、console
warning/error、page error 和 Browser HTTP 错误计数均为 0。最终 8 张截图已逐张人工复核，桌面和 390px
下的候选状态、owner、审批链接、培养 Case 与 stale 状态均可见且无裁切：

- `output/playwright/t12-catalog-product-proposal/01-no-policy-desktop.png`
- `output/playwright/t12-catalog-product-proposal/02-no-policy-390.png`
- `output/playwright/t12-catalog-product-proposal/03-policy-pending-desktop.png`
- `output/playwright/t12-catalog-product-proposal/04-policy-approval-deep-link.png`
- `output/playwright/t12-catalog-product-proposal/05-active-policy-proposal-desktop.png`
- `output/playwright/t12-catalog-product-proposal/06-active-policy-proposal-390.png`
- `output/playwright/t12-catalog-product-proposal/07-queued-cultivation-desktop.png`
- `output/playwright/t12-catalog-product-proposal/08-stale-and-cultivation-390.png`

t12 目录只有这 8 张 PNG。本任务没有触碰既有未跟踪 `output/playwright/t10-*`。

## 完整验证矩阵

| 安全命令 | 结果 |
| --- | --- |
| `PATH="$PWD/.venv/bin:$PATH" python3 scripts/check_boundaries.py` | PASS；7 项结构检查全通过 |
| `.venv/bin/python scripts/scan_sensitive.py` | PASS |
| `.venv/bin/python -m ruff check .` | PASS |
| `.venv/bin/python -m mypy domains shared tool_gateway apps workflows notification_gateway infra` | PASS；515 个 source file 无问题 |
| `.venv/bin/python -m pytest tests/integration -q -x -rs` | PASS；1729 passed，2086.40s |
| `TRADEOS_REQUIRE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_catalog_product_proposal_controlled.py -q -rs` | PASS；1 passed，19.96s |
| `.venv/bin/python -m pytest -q -rs` | PASS；8741 passed，1884.57s |
| `cd apps/web && npm test` | PASS；26 files、335 tests，9.18s |
| `cd apps/web && npm run typecheck` | PASS |
| `cd apps/web && npm run lint` | PASS；0 error、172 warning（既有规则输出） |
| `cd apps/web && npm run build` | PASS；151 modules transformed，475ms |
| `cd apps/web && PATH="$PWD/../../.venv/bin:$PATH" npm run gen:api` | PASS；1.4s |
| `cd apps/web && git diff --exit-code -- src/api/api.d.ts` | PASS；无 diff |
| `git diff --check` | PASS |

integration run 的 `-x` 在零失败时不会提前退出；最终 full run 已按验收要求不带 `-x` 完整执行。
仓库 `.venv` 为 Python 3.12.14；裸系统 `python3` 无法解析仓库使用的 Python 3.12 语法，所以验证命令
显式使用 `.venv`。

## 九条硬边界与人工审计

1. 模型没有接触凭证；受控测试不读 secret，外部端口计数全部为 0。
2. 本变更没有金额计算或 `float` 业务值，也没有报价或价格承诺。
3. 评估使用固定规则状态，没有模型概率或数值置信度。
4. 合成国家和品类事实带合格 Provenance；事件、workflow context 与日志不复制事实正文。
5. Account/Need 事实、策略决定和 proposal 推断分存不同结构；proposal 始终显示候选警告。
6. 没有联系人或发送序列，更没有把未验证可达性放入序列。
7. 没有 indicative/quoted 价格、Quote 或客户可见报价。
8. 所有受控行都绑定 tenant；测试计数和读取显式过滤 tenant。
9. 没有生产反向导入；Products 不直接导入 Demand，跨域事实只经显式 workflow DTO/事件传递。

额外审计确认：不存在自批、stale 绕过或客户/供应商界面；重投和 runtime 重启都先读 canonical 行再收敛。

## 残余生产风险和未运行项

- 受控的 3 Account/单一品类只证明工程链路，没有校准生产门槛，也没有证明 Kenya 对 three-wheelers
  有真实需求。生产前仍需真实样本、偏差分析、误报/漏报成本和独立策略审批。
- `CatalogCultivationQueued` 当前没有本子项目内的下游 subscriber，会按既有失败关闭规则成为
  `no registered handler` dead。后续需独立实现培养消费者与恢复策略，不能把排队误称为已执行培养。
- 仓库尚无 Account country-correction 公开命令；生产更正入口及其审批/事件语义需另行实现和验收。
- 真实外部 provider、供应商/客户联系、真实 Quote/价格承诺、生产策略激活、培养队列下游和实际成交仍为
  `not_run`/未完成。
- 前端 lint 仍报告 172 个既有 warning；不阻断本子项目，但应单独治理。
- 外挂盘会生成 ignored `._*` AppleDouble；Git 仍会报告既有 `._pack-*.idx` non-monotonic warning。
  本任务没有删除或修改共享 `.git/objects`。

```yaml
push: not_run
merge: not_run
deploy: not_run
production_activation: not_run
model_eval_work: not_applicable
```
