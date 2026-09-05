# Phase2 Catalog Product Proposal 验收记录

## 结论与范围

Catalog Product Proposal 子项目的受控真实核心与 Browser 功能链已经跑通，但最终完成门禁尚未通过：
390px 提案卡仍有一处可见裁切，且全仓 integration/full suite 仍为红灯。当前结论只表示：
在一个隔离本地租户内，经独立人工审批的显式策略可以把符合确定性规则的 Need Cluster 生成
内部候选提案，第二次独立审批最多创建一个 `queued` 培养 Case。它不表示整个 Phase2 完成，
不表示正式 Product、供应确认、可报价价格、真实市场需求或生产启用。

- 实施基线：`2cb751ee71bee796754ce8e1e65bfecc8978b318`。
- 最终验证 HEAD：`3dc692d8bf761aa90aa0728c71242ab93eaf7b73`（不含本验收提交）。
- 工作树：`phase2-catalog-product-proposal` 隔离 worktree。
- 受控核心：真实 PostgreSQL migrations/SQLAlchemy repository、transactional Outbox、Workflow
  engine、scheduler advisory lock cycle/runtime 重建、FastAPI、Vite 与 Chromium。
- 只有 Account、Validated Need、Need Cluster、合格 Provenance 和 product Employee 是隔离合成输入；Catalog
  policy/evaluation/proposal/cultivation 与 Approval 业务行都经生产端口创建和推进。
- 本轮没有 merge、push、deploy、生产策略激活或真实外部行为。

```yaml
controlled_postgres_api_scheduler_browser: passed
real_external_provider_calls: not_run
real_supplier_contact: not_run
real_customer_contact: not_run
real_quote_or_price_commitment: not_run
production_policy_activation: not_run
```

Prompt、model 和 eval 数据集未改变；model/eval 工作记为 `not_applicable`。

## RED → GREEN

### RED

先新建真实核心 E2E，启动迁移后的 PostgreSQL、Uvicorn API、Vite 和真实 Chromium，确认页面在无策略时
显示“未配置即关闭”，然后在尚未完成的受控链标记处显式失败。命令与结果：

```text
TRADEOS_REQUIRE_E2E=1 .venv/bin/python -m pytest tests/e2e/test_catalog_product_proposal_controlled.py -q -rs
1 failed in 15.24s
```

这是验收 harness 缺失的真实 RED，不是已有生产功能的缺陷。补齐链路后的首次运行又证明中央审批
Evidence 契约会拒绝不可路由的伪消息 ID；验收 seed 因此改用真实 `msg_<ULID>` 格式，没有放宽生产校验。

### GREEN

最终单用例运行结果：`1 passed in 12.33s`。它单独证明了：

1. 三个独立 Account 各绑定一个 Validated Need，品类精确为 `three-wheelers`，国家事实为 `KE`，
   country/category Evidence 都有可路由 conversation ID 和已确认 Provenance。这些是合成验收事实，不是
   Kenya 市场结论。
2. 重复 membership 事件和 scheduler cycles 在无活动策略时产生 0 policy/evaluation/proposal/case。
3. product 员工通过真实 HTTP 只提交 `minimum_distinct_accounts=3`、其他可选门槛为 `null`、
   `require_unified_unit=false`；不同 boss 经中央 Approval 页的精确深链决定。
4. 通过评估精确绑定 3 个 Account/Need 和 1 个 `KE`。`membership_integrity` 与
   `distinct_accounts` 为 `passed`；国家事实完整且门槛未启用，故 `distinct_countries=not_required`；
   复购、数量/单位与统一单位事实缺失，仍分别为 `unknown`，但在该显式策略下不阻断。
5. 提案批准只创建 1 个 `queued` 培养 Case。同一 Approval 事件在 runtime 重启前后多次重投，
   同一快照始终只有 1 evaluation、1 proposal、1 Approval、1 Case。
6. 一次绑定 country 事实变化生成新 pending snapshot；在它决定前再改变 canonical 事实，老 Approval
   应用时重读事实并转 `stale`，培养 Case 仍精确为 1。恢复后的当前事实依幂等键生成第三份
   evaluation/proposal snapshot，不改写前两份。
7. 已存在和随机不存在的提案在错租户下都返回同样的 403/body，sales 读取和策略提交也是 403。
   Browser 的两个批准请求体都只有 `{"decision":"approve"}`，策略/决策 body 都不包含 tenant、actor、
   facts hash、Provenance、owner 或 approver。

## 零外部效果证据

前后都以真实表计数，并与受控传输计数交叉核对：

| 事实/能力 | 前 | 后 |
| --- | ---: | ---: |
| formal Product | 0 | 0 |
| Supplier | 0 | 0 |
| Prospect Contact | 0 | 0 |
| Contact Point | 0 | 0 |
| Sourcing Search Execution | 0 | 0 |
| Outreach Message Attempt | 0 | 0 |
| Quote | 0 | 0 |
| durable Tool Gateway Call | 0 | 0 |
| Tavily usage/search | 0 / 0 | 0 / 0 |
| public-page validate/fetch | 0 / 0 | 0 / 0 |
| model | 0 | 0 |
| real network | 0 | 0 |

没有造数价格、供应商、联系人、搜索、发送或 Product promotion 来让测试通过；也没有读取、打印或复制
任何 secret value。

## Browser 与截图人工检查

Browser 使用 fresh Chromium context 和真实 Vite/FastAPI 请求，完整穿越 Product Center → 精确
Approval Center → Product Center。桌面与 390px 页面级断言均为 `scrollWidth <= innerWidth`，错误 overlay、
console warning/error、page error 和 Browser HTTP 错误计数均为 0。逐张人工复核发现该断言仍有盲区：
`06-active-policy-proposal-390.png` 的 pending proposal 卡右侧 `pending_review` badge 与 owner ULID 有可见
裁切；其余 7 张可接受。目标提案/培养卡均已滚动到截图视口内，不以离屏 DOM 元素冒充可见证据。
因此当前 8 张是完整缺陷证据，但不能称为 8 张最终稳定视觉验收图；前端修复后必须重拍并逐张复核。

- `output/playwright/t12-catalog-product-proposal/01-no-policy-desktop.png`
- `output/playwright/t12-catalog-product-proposal/02-no-policy-390.png`
- `output/playwright/t12-catalog-product-proposal/03-policy-pending-desktop.png`
- `output/playwright/t12-catalog-product-proposal/04-policy-approval-deep-link.png`
- `output/playwright/t12-catalog-product-proposal/05-active-policy-proposal-desktop.png`
- `output/playwright/t12-catalog-product-proposal/06-active-policy-proposal-390.png`
- `output/playwright/t12-catalog-product-proposal/07-queued-cultivation-desktop.png`
- `output/playwright/t12-catalog-product-proposal/08-stale-and-cultivation-390.png`

t12 目录只有这 8 张 PNG，没有 trace、临时截图或 AppleDouble 证据。本任务没有触碰既有未跟踪
`output/playwright/t10-*`。

## 完整验证矩阵

| 安全命令 | 结果 |
| --- | --- |
| `PATH="$PWD/.venv/bin:$PATH" python3 scripts/check_boundaries.py` | PASS；7 项结构检查全通过 |
| `python3 scripts/scan_sensitive.py` | PASS |
| `.venv/bin/python -m ruff check .` | PASS |
| `.venv/bin/python -m mypy domains shared tool_gateway apps workflows notification_gateway infra` | PASS；515 个 source file 无问题 |
| `.venv/bin/python -m pytest tests/unit -q` | PASS；6983 passed，74.84s |
| `.venv/bin/python -m pytest tests/integration -q` | FAIL；1674 passed、55 failed、1 teardown error，1117.10s |
| `.venv/bin/python -m pytest tests/e2e/test_catalog_product_proposal_controlled.py -q -rs` | PASS；1 passed，12.33s |
| `.venv/bin/python -m pytest -q` | FAIL；8683 passed、57 failed、1 teardown error，1784.35s |
| `cd apps/web && PATH="$PWD/../../.venv/bin:$PATH" npm run gen:api` | PASS；生成 1.6s |
| `cd apps/web && git diff --exit-code -- src/api/api.d.ts` | PASS；无 diff |
| `cd apps/web && npm test` | PASS；26 files、334 tests，11.62s |
| `cd apps/web && npm run typecheck` | PASS |
| `cd apps/web && npm run lint` | PASS；0 error、172 warning（既有规则输出） |
| `cd apps/web && npm run build` | PASS；151 modules transformed |
| `git diff --check` | PASS |

完整门禁没有通过，不能把这个报告解释为全仓可合并绿灯。integration/full 的失败清单不包含本任务的
`test_catalog_product_proposal_controlled.py`；失败分为：两个既有 Slice 4 demo、迁移 downgrade 测试被
Catalog 已持久事实保护阻断后的 schema/outbox 级联、Need Unit 临时数据库 teardown 的
`ObjectInUseError`，以及 sourcing runtime 对新增 `NeedClusterMembershipChanged` handler 的 delivered/dead
期望不一致。全量还多出两个既有 E2E：`test_slice4_manual_send_fixed_journey` 与
`test_need_cluster_sourcing_admission_real_core_is_bounded_and_recoverable`。隔离运行 migration
base→head roundtrip 曾得到 `1 passed in 6.76s`，而 Slice 4 demo 隔离稳定失败（`1 failed in 7.45s`），
支持“存在顺序污染，但并非全部失败都只是污染”的判断；这些根因必须另任务修复，不能在本验收中越权改生产
或共享 fixture。

Brief 要求的最终全量命令是 `.venv/bin/python -m pytest -q -rs`。本轮实际完整运行少了仅影响 skipped
原因展示的 `-rs`，行为矩阵仍有效，但根据验收裁定不冒充最终完成门禁，也不再重复同一已知失败的 30 分钟
矩阵；待上述根因修复并冻结新 HEAD 后，必须重新执行 brief 的精确命令。

`npx` 存在。计划中绑定的
`/Users/xueziheng/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3` 虽然是 Python
3.12.14，但 `import fastapi` 失败；这是计划运行时环境与仓库依赖不匹配，不记为通过。全部验证使用
仓库 `.venv/bin/python` 的 Python 3.12.14。裸 `python3 scripts/check_boundaries.py` 会落到 macOS Python
3.9 并因 Python 3.12 泛型语法解析失败；上表显式把仓库 `.venv/bin` 放在 `PATH` 首位后通过。裸
`npm run gen:api` 同样因内部 Python 找不到 FastAPI 失败，上表记录的是相同 PATH 修正后的真实 PASS。

## 九条硬边界与额外人工审计

1. 模型没有接触凭证；受控测试不读 secret，外部端口计数全部为 0。
2. 本变更没有金额计算或 `float` 业务值，也没有报价/价格承诺。
3. 评估是固定规则状态，没有模型概率或数值置信度。
4. 合成国家和品类事实带合格 Provenance；事件、workflow context、日志与报告都不复制事实原文或
   Provenance 正文。
5. Account/Need 事实、策略决定和 proposal 推断分存不同结构；proposal 始终显示候选警告。
6. 没有联系人或发送序列，更没有把未验证可达性放入序列。
7. 没有 indicative/quoted 价格、Quote 或客户可见报价；页面也不提供生成报价动作。
8. 所有受控行都绑定 tenant；测试计数/读取显式过滤 tenant，错租户的已存在/不存在主体同样拒绝。
9. 变更没有生产导入；人工核对 Products 未直接导入 Demand，跨域事实只经显式 workflow DTO/事件。

额外审计确认：没有 Demand↔Products 直接 import；事件/日志/错误/workflow context 没有事实正文；没有自批、
stale 绕过或客户/供应商界面；重投和 runtime 重启都先读 canonical 行再收敛，没有用新主体补造。

## 残余生产风险和未运行项

- 受控的 3 Account/单一品类只证明工程链路，没有校准生产门槛，没有证明 Kenya 对 three-wheelers
  有需求。上线前仍需真实样本、偏差分析、误报/漏报成本和独立策略审批。
- `CatalogCultivationQueued` 是已持久的 metadata-only 输出，但当前 scheduler outbox registry 没有下游
  subscriber。受控运行因此观察到该事件被标记为固定 `no registered handler` dead；这不重复 Case，也没有
  触发外部动作，但会产生死信/告警噪声并无法为未来培养消费者自动补投。应在后续培养子项目中增加
  经独立审查的精确消费者/恢复策略，不能放宽全局 no-handler 失败关闭。
- 仓库尚无 Account country-correction 公开命令，所以 stale 验收在隔离 fixture 内持有行锁并更新该事实；
  第一次变化发布真实 metadata-only `AccountCountryFactsChanged`，第二次变化特意不发新事件，证明决定应用时仍会
  重读 canonical 事实。生产更正入口与其审批/事件语义需另行实现和验收。
- 真实外部 provider、供应商/客户联系、真实 Quote/价格承诺、生产策略激活、培养队列下游与实际成交还是
  `not_run`/未完成。
- 390px 的 pending proposal 卡存在可见裁切；页面级 `scrollWidth` 断言没有捕获卡内 badge/长 ULID
  的局部溢出。前端修复、重拍和人工复核完成前，视觉验收不得标绿。
- 外挂盘会生成 ignored `._*` AppleDouble；t12 最终目录已清理，Git 仍会报既有 `._pack-*.idx`
  non-monotonic warning。本任务没有删除或修复共享 `.git/objects`。

```yaml
push: not_run
merge: not_run
deploy: not_run
production_activation: not_run
model_eval_work: not_applicable
```
