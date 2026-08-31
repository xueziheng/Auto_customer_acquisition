# Phase2 Sourcing Case V2 验收记录

## 结论与范围

本记录确认“免费公开寻源 → 候选产品卡 → 人工审核 → `ESTIMATED` 成本交接”这个子项目已通过受控验收；**不表示整个 Phase 2 完成**。NeedCluster 排序、联系人多源瀑布、70/30 分配、自动背压、真实 direct supplier quote 和商业来源仍需独立规格、实现、样本/策略数据和逐次授权。

- 基线：`61c4d7538273d06cf07ac99a1b1620e55415350b`。
- 运行环境：`/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python`、本地 Docker PostgreSQL/MinIO、真实 FastAPI/Vite/scheduler。
- 验收所涉窄修复：`a4c0bf7`、`5c62c21`、`f774be8`、`8248bf9`、`85788b4`、`415892a`、`d82019f`、`3ad584b`、`c9f8abd`、`c8d1506`；各修复与本文件的文档提交分开。
- 受控端口严格限于 Tavily transport、public-page transport、extraction model。PostgreSQL migration/table、领域服务、V2 Workflow、Tool Gateway、Outbox、API composition/read projection 均为真实实现。

## RED → GREEN 与根因修复

最初的受控 E2E 是真实 RED：真实 PostgreSQL/API/Vite/scheduler 已启动，但 V2 handler `sourcing_case.v2.check_ladder` 没有注册。随后完整链又依次暴露 JSON array 到 tuple 的 API 契约、V2 review/handoff handler 未装配、候选核验把可选 Need `model` 当成必填、共享 E2E fixture 的 XZ policy 污染，以及 Linux source image 缺少稳定本地标识等问题。

这些均在责任模块修复并各自提交；没有在 repository/domain/workflow/outbox/API 中使用 fake 绕过。最后受影响回归为：

```text
tests/e2e/test_country_policy_settings.py
tests/e2e/test_sourcing_case_controlled.py
tests/integration/test_evidence_parser_linux.py
tests/integration/test_quote_source_readers_linux.py
4 passed in 180.38s
```

Linux gate 使用已存在且经 ARM64/Linux 校验的本地固定 image；构建路径 `network_mode=none`。环境变量缺失时只选该固定 image，显式无效值仍硬失败，不转为 skip/not_run。

## 受控真实核心链与外部边界

真实 E2E 经 Demand service/Outbox 创建 completeness `3` 的 Validated Need，触发一个 canonical V2 Case/Run，记录 rung 1–5 的 no-match 事实；再经实际 HTTP 草拟、hash 确认并运行计划。它持久化公开 Artifact/draft，核验 Candidate，投影 `source_only` Product 与 Supply Option，提交并由 boss 精确确认 review，创建 Opportunity 后交接一个 `ESTIMATED` CostSheet（`product_purchase` 为 Decimal wire string、`indicative` basis）。

受控成功链的 durable ToolCall 精确为 `web.search`、`web.read_page` 各一次；不存在其他 ToolCall。因此 contact discovery、contact/email verification、email/send、procurement request 和客户 Quote 均为 `0`。控制计数为 Tavily usage/search `1/1`、page fetch `1`、model `1`、真实网络 `0`。页面指令没有执行。

受控 quota account 从 10 个安全免费额度、`usage_used=0` 到可用 9、`usage_used=1`，没有付费回退。额度卡的 `reservations=1` 是账户级、不可释放的保守累计扣减；Run 的 `reserved_credits=0` 是本 Run 没有仍处于 reserved/uncertain 的请求，二者不是冲突的同一计数。

真实 Tavily 和供应商页面本轮固定不运行：

```yaml
real_tavily_supplier_pages: not_run
reason: explicit_market_category_and_research_budget_not_provided
```

未读取、打印或记录 Tavily secret value；“环境已配置”也没有被视为联网授权。真实模型、联系、邮件、采购和客户 Quote 均未调用。

受控 composition 还需要一个 scheduler audience resolver；仓库目前没有产品通知适配器，E2E 注入的 no-op resolver 是**本路径没有触发的 required composition dependency**，不是寻源外部端口，也没有赋予任何通知业务语义。独立审查应据此判断 P84 的可接受性。

## 完整门禁

| 命令 | 实际结果 |
| --- | --- |
| `ruff check .` | PASS，`All checks passed!` |
| `mypy domains shared tool_gateway apps workflows notification_gateway infra` | PASS，`Success: no issues found in 491 source files` |
| `python3 scripts/check_boundaries.py` | PASS，7 项结构检查全部通过 |
| `python3 scripts/scan_sensitive.py` | PASS，退出码 0 |
| `pytest -q -rs` | **`7927 passed in 1793.22s (0:29:53)`；0 failed，0 skipped**；单一自然结束进程 |
| `pytest tests/integration/test_sourcing_migrations.py tests/integration/test_migrations.py tests/unit/test_work_intake_migration_head.py -q -rs` | PASS，81 passed，68.22s |
| Task13 root：scheduler/runtime/outbox/run tests | PASS，75 passed，22.57s |
| Task14 root：sourcing/products router tests | PASS，9 passed，3.88s |
| Task14 web：sourcing/product/run tests | PASS，3 files、47 tests，2.35s |
| `npm run gen:api` | PASS（需项目 Python 3.12 PATH） |
| `npm test` | PASS，23 files、281 tests |
| `npm run typecheck` / `npm run build` | PASS / PASS |
| `npm run lint` | PASS，0 errors；195 个既有 style warnings |

裸 `npm run gen:api` 会因系统 Python 缺少 FastAPI 而失败；这只是未使用项目 Python PATH 的环境 RED，不是生成器或应用代码缺陷。正式门禁已使用上述项目 Python PATH。

## Browser-first 数据态证据

使用独立真实生命周期保留非空受控数据，再以 Browser 首先访问本地 Vite/API；没有使用仓库 Playwright 作为回退。桌面宽度为 1249px，390px 移动视口也分别验证 `scrollWidth == width`，无 Vite overlay、Browser console error 为零。

| 页面/场景 | Browser 看到的真实数据 |
| --- | --- |
| `/sourcing` | 非空 V2 `discovering` queue Case，rung 至 5，点击目标为 Case detail。 |
| `/sourcing/:caseId` | `handed_to_costing` Case、五级检查、页面 Artifact 事实/自述/推断/未知分栏、四项规格比较、八项核验、qualified Candidate、`indicative` 不可报价警示、plan hash/free quota、boss 已确认 review、uncertain 为 0。 |
| `/products` | 非空 `source_only` Candidate card、Artifact 来源链、逐项规格与“不可用于客户报价”。 |
| `/crm/opportunities` 与 `/costing-quotes` | Task 15 Opportunity 与一份 `ESTIMATED v1`（500 件、USD）成本表和 `indicative` 产品采购项。Quote 依赖未装配时显示局部 `dependency_unavailable`，没有把它伪装为空数据或触发 Quote。 |
| `/runs` | 已完成 V2 的 8 步、候选 1、search/page 各 1、免费消耗 1、uncertain 0，且工具只显示两个安全 ToolCall；没有 workflow context、网页原文或联系人。 |
| 不存在 Case | 404 明确显示“案例不存在或不属于当前租户”，不是空队列。 |

现有 Task14 前端 47-test gate 还覆盖计划替换/旧确认失效、free/paid/unknown/exhausted 的按钮状态、rejected Candidate、primary/alternate UX 限制、uncertain recovery 和 projection 403/503 的局部错误显示。受控成功数据没有制造不确定请求，故 Browser live view 正确显示 0 项而非虚构 recovery。

## 未测试项、警告与保留物

- P85 所列的真实 Tavily/真实供应商页面、真实模型、真实直接供应商报价、真实联系人/邮件/采购、客户 Quote 均未测试且未调用；它们需要新的明确授权。
- Browser 生命周期和测试日志均在 Browser session 或 `/tmp`，未写入仓库。全部未跟踪 `output/playwright/t10-*` 目录原样保留、未暂存、未删除：`t10-188cce469d99479da14bce6443503661`、`t10-46d14f488a2b412b9fa21fe89473342b`、`t10-5aa94ee5c00b4796a91ce70ddfb4a43b`、`t10-607358735a1249e9874605d3895b09f8`、`t10-60c68cbcfe11447ea5603236102bac21`、`t10-7382073af5e046b4ae72b39c0eb83f1c`、`t10-9d527eb01e0f4de5a68f7fbbc53fb754`、`t10-a320c0334c5f4c6ebf78e67ef47d370e`、`t10-ad2ec8cfce144ac49bea4d7979ff4b0a`、`t10-b920f6b4629d40b0a9409f1237222407`、`t10-c0da091c5b66450e9068a38fb707c6b5`、`t10-c1fb347fa5504e7cbb8c41ef1ccc35c5`、`t10-c3f364b9ff7b4d2683637cc60ae97508`、`t10-d1c9b9a89f0b49769b57ff9b7ce4619f`、`t10-d6eda81d25e14d9680de58438471cd65`、`t10-dbd3b59a412d411ab15fdf47feaccfe6`、`t10-ea989ed470ca4693868be7226c639bdb`、`t10-f429e69bbc4a4befb6e612d34b28baa9`、`t10-fe53163de0834f6e99236adac7e531d0`。
- Git 每次读取可能报告已有 AppleDouble `._pack-*.idx` non-monotonic warning；没有清理或修改任何 `.git/objects` 文件。
- lint 的 195 个 warning 保持为 warning，未降低 lint/scanner/mypy/boundary 规则。
