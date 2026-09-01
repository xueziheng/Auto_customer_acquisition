# Phase2 Sourcing Case V2 验收记录

## 结论与范围

本记录确认“免费公开寻源 → 候选产品卡 → 人工审核 → `ESTIMATED` 成本交接”子项目已通过受控验收和最终完整门禁；**不表示整个 Phase 2 完成**。NeedCluster 排序、联系人多源瀑布、70/30 分配、自动背压、真实 direct supplier quote 和商业来源仍需独立规格、实现、样本/策略数据和逐次授权。

- 基线：`61c4d7538273d06cf07ac99a1b1620e55415350b`。
- 运行环境：`/Users/xueziheng/miniconda3/envs/tradeos-py312/bin/python`、本地 Docker PostgreSQL/MinIO、真实 FastAPI/Vite/scheduler。
- 验收所涉窄修复：`a4c0bf7`、`5c62c21`、`f774be8`、`8248bf9`、`85788b4`、`415892a`、`d82019f`、`3ad584b`、`c9f8abd`、`c8d1506`、`fd4b585`、`feccba0`、`56d0c0f`、`c0e097b`、`3726f7e`、`61c075e`、`681562d`、`4b68446`；各产品/基线修复与本文件的文档提交分开。
- 受控端口严格限于 Tavily transport、public-page transport、extraction model。PostgreSQL migration/table、领域服务、V2 Workflow、Tool Gateway、Outbox、API composition/read projection 均为真实实现。

## RED → GREEN 与根因修复

最初的受控 E2E 是真实 RED：真实 PostgreSQL/API/Vite/scheduler 已启动，但 V2 handler `sourcing_case.v2.check_ladder` 没有注册。随后完整链又依次暴露 JSON array 到 tuple 的 API 契约、V2 review/handoff handler 未装配、候选核验把可选 Need `model` 当成必填、共享 E2E fixture 的 XZ policy 污染、Linux source image 依赖作者机 image ID，以及 Opportunity 缺失后的 handoff/retry 持久化缺口。

最新一次自然结束的全量 pytest 又提供了一条独立 RED：`tests/e2e/test_country_policy_settings.py::test_real_country_policy_settings_approval_and_activation` 使 `ApprovalDecided` 的 `notification.approval_decided` delivery 因 E2E audience 的 `AssertionError` 进入 dead outbox。根因不是寻源 handler；完整 scheduler 实际订阅了该审批事件。`4b68446` 把这个必需的通知 composition dependency 改为记录严格类型的 `(tenant_id, DomainEvent)` 并明确返回零收件人，保留真实 NotificationProjectionHandler/Outbox。国家政策 E2E 断言一条 tenant-bound `ApprovalDecided` 调用且无死信；受控寻源链断言调用增量为零。它不替代、模拟或触发任何通知/邮件通道。

这些均在责任模块修复并各自提交；没有在 repository/domain/workflow/outbox/API 中使用 fake 绕过。最后受影响回归为：

```text
pytest tests/e2e/test_country_policy_settings.py::test_real_country_policy_settings_approval_and_activation -q -rs
1 passed in 11.31s
pytest tests/e2e/test_sourcing_case_controlled.py::test_controlled_need_to_estimated_cost_uses_real_core_only -q -rs
1 passed in 13.37s
```

Linux source gate 从可审计的本地依赖 image 重建当前源码层，生成并校验 source manifest/hash、OS/arch；Docker 构建使用 `network_mode=none`。它不依赖作者机固定 source image ID、不 pull、不联网；找不到经审计依赖 image 会硬失败，不转为 skip/not_run/mock。

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

受控 composition 还需要一个 scheduler audience resolver。它不是寻源外部端口：完整 scheduler 的真实 `ApprovalDecided → notification.approval_decided` 投影会调用它；E2E 实现仅记录调用、返回空收件人，不创建通知任务或外部行为。国家政策路径已验证这条真实组合路径；受控寻源路径的调用增量为 `0`。独立审查应据此判断 P84 的可接受性。

## 完整门禁

| 命令 | 实际结果 |
| --- | --- |
| `ruff check .` | PASS，`All checks passed!` |
| `mypy domains shared tool_gateway apps workflows notification_gateway infra` | PASS，`Success: no issues found in 491 source files` |
| `python3 scripts/check_boundaries.py` | PASS，7 项结构检查全部通过 |
| `python3 scripts/scan_sensitive.py` | PASS，退出码 0 |
| `pytest -q -rs` | 上一自然结束进程为 **`1 failed, 7932 passed in 2701.34s (0:45:01)`**；根因修复后，最终单一自然结束进程为 **`7933 passed in 3321.54s (0:55:21)`**，0 failed，0 skipped（无 skipped 区段）。 |
| migrations root | PASS，81 passed，69.89s，0 skipped |
| Task13 root：scheduler/runtime/outbox/run tests | PASS，75 passed，28.00s，0 skipped |
| Task14 root：sourcing/products router tests | PASS，9 passed，3.53s，0 skipped |
| `npm run gen:api` / `npm test` / `npm run typecheck` / `npm run build` / `npm run lint` | 全部 PASS；Vitest 23 files、282 tests；lint 0 errors、195 warnings。 |

裸 `npm run gen:api` 会因系统 Python 缺少 FastAPI 而失败；这只是未使用项目 Python PATH 的环境 RED，不是生成器或应用代码缺陷。正式门禁必须使用上述项目 Python PATH。

## Browser-first 数据态证据

使用独立真实生命周期保留非空受控数据，再以 Browser 首先访问本地 Vite/API；没有使用仓库 Playwright 作为回退。桌面及 390px 移动视口均验证 `scrollWidth == width`，无 Vite overlay、Browser console error 为零。屏幕证据和临时生命周期脚本仅留在 Browser session 或 `/tmp`。

| 页面/场景 | Browser 看到的真实数据 |
| --- | --- |
| `/sourcing` | 非空 V2 `discovering` queue Case，rung 至 5，点击目标为 Case detail。 |
| `/sourcing/:caseId` | 非空 V2 Case、五级检查、页面 Artifact 事实/自述/推断/未知分栏、四项规格比较、八项核验、qualified Candidate、`indicative` 不可报价警示；先观察 `opportunity_required`，创建 Opportunity 后只点击一次 retry，进入 `handed_to_costing`。 |
| `/products` | 非空 `source_only` Candidate card、Artifact 来源链、逐项规格与“不可用于客户报价”。 |
| `/crm/opportunities` 与 `/costing-quotes` | Task 15 Opportunity 与一份 `ESTIMATED v1`（500 件、USD）成本表和 `indicative` 产品采购项。Quote 依赖未装配时显示局部 `dependency_unavailable`，没有把它伪装为空数据或触发 Quote。 |
| `/runs` | 已完成 V2 的 8 步、候选 1、search/page 各 1、免费消耗 1、uncertain 0，且工具只显示两个安全 ToolCall；没有 workflow context、网页原文或联系人。 |
| 不存在 Case | 404 明确显示“案例不存在或不属于当前租户”，不是空队列。 |

Browser 的真实 API 数据态还逐项走过：替换 plan 后旧确认 API 精确拒绝而新 hash 可确认；free、paid、unknown、exhausted 都显示不同的 run gate；受控 Tavily 503 保留一条 `UNCERTAIN RECOVERY 1`；把真实员工角色切换为 product 得到局部 403；底层额度存储不可用 fail-closed 映射为 unknown。最后一项不是普通应用 HTTP 5xx，因当前产品没有安全可触发的普通 5xx endpoint，故不把它写成已验收 5xx。Browser live handoff 的实际外部计数为 Tavily/search `1`、page `1`、model `1`，contact/email/send/procurement/Quote `0`。

## 未测试项、警告与保留物

- P85 所列的真实 Tavily/真实供应商页面、真实模型、真实直接供应商报价、真实联系人/邮件/采购、客户 Quote 均未测试且未调用；它们需要新的明确授权。
- Browser 生命周期和测试日志均在 Browser session 或 `/tmp`，未写入仓库。全部 29 个未跟踪 `output/playwright/t10-*` 目录原样保留、未暂存、未删除：`t10-07fa7625fd954e77906e0e2f57e1d393`、`t10-188cce469d99479da14bce6443503661`、`t10-18d07560bb8e494daa654c02bf232061`、`t10-1f080820958a4998971724ba64cddd89`、`t10-44b684564b704ca19fe27b575c0de355`、`t10-46d14f488a2b412b9fa21fe89473342b`、`t10-47a087010a954f1fbbe34e76db7fb417`、`t10-5aa94ee5c00b4796a91ce70ddfb4a43b`、`t10-5f7a69c01ac6431e829d76251537ed71`、`t10-607358735a1249e9874605d3895b09f8`、`t10-60c68cbcfe11447ea5603236102bac21`、`t10-7382073af5e046b4ae72b39c0eb83f1c`、`t10-7464bef9b74344669179b3ac04fa6392`、`t10-7726591a7b4f4479bfecbaed8bea8068`、`t10-9d527eb01e0f4de5a68f7fbbc53fb754`、`t10-a320c0334c5f4c6ebf78e67ef47d370e`、`t10-ad2ec8cfce144ac49bea4d7979ff4b0a`、`t10-b084421611a8409e98e6c53f4944c34f`、`t10-b920f6b4629d40b0a9409f1237222407`、`t10-c0da091c5b66450e9068a38fb707c6b5`、`t10-c1fb347fa5504e7cbb8c41ef1ccc35c5`、`t10-c3f364b9ff7b4d2683637cc60ae97508`、`t10-d1c9b9a89f0b49769b57ff9b7ce4619f`、`t10-d6eda81d25e14d9680de58438471cd65`、`t10-dbd3b59a412d411ab15fdf47feaccfe6`、`t10-ea989ed470ca4693868be7226c639bdb`、`t10-ede41480cb5147e2b9595fad52f88852`、`t10-f429e69bbc4a4befb6e612d34b28baa9`、`t10-fe53163de0834f6e99236adac7e531d0`。
- Git 每次读取可能报告已有 AppleDouble `._pack-*.idx` non-monotonic warning；没有清理或修改任何 `.git/objects` 文件。
- lint 的 195 个 warning 保持为 warning，未降低 lint/scanner/mypy/boundary 规则。
