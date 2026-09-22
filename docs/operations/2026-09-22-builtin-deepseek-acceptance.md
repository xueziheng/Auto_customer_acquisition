# 内置 DeepSeek Agent 首批验收

日期：2026-09-22。分支 `codex/builtin-deepseek-agent`，执行基线
`c8251c15d6502e6cfc19a28a3e2c61d5c022db53`。本记录正在收敛最终回归与独立审查，不能据此宣称公司部署完成。

## 已获得的证据

- 后端 unit：`python -m pytest tests/unit -q`，7454 passed，102.96 秒。
- 业务评估：`python -m pytest tests/evals -q`，25 passed；其中新增 12 个内置助手安全/澄清案例。
  模型输出受控，不能代表 live 模型准确率。
- `python -m mypy domains shared tool_gateway connectors`：335 个源码文件通过。
- 结构自检通过；`python -m ruff check .` 通过。
- 受控独立 API/PG/scheduler、显式 probe、多轮提案、精确确认、研究来源与 unknown 不重试已逐任务测试。
- 1440/390px 浏览器会话操作已验证；HTTP 受控，真实服务端授权/数据库持久性由独立集成测试覆盖。
- 最终 integration、Web 与受影响端到端正在运行，结果将在完成后补齐。

真实 DeepSeek 调用、真实搜索/页面和共享部署均为 `not_run`，原因是没有管理员提供的真实密钥、
明确调用额度及获准研究输入。未读取真实 `.env`、未启动共享网址、未发送客户消息。

## 回归中发现的问题

初次全库 unit 为 7430 passed / 18 failed。修正 API 路由/422 契约、旧报价生命周期测试的既有
handoff 兼容性读取替身和受环境代理影响的 loopback 测试；安装缺失 Chromium headless shell 后全绿。
初次 integration 在 626 passed / 11 failed / 3 errors 时停止诊断；新不可变会话事实污染共享库的
旧迁移往返，已改用隔离数据库，未放宽生产不可变约束。旧迁移 head、固定日期滚动窗口与 handler
预期已同步；Linux 测试镜像漏掉既有 handoff 模块和新增模型装配模块，已补显式源码白名单。

新增回归证明：密钥解析失败发生在发出请求前，调用账本应为 rejected 且释放额度；先观察到 invalid
失败，再修正为本地 prepare→mark_dispatched→单次 HTTP，测试通过。真实验收脚本对 probe/Run
失败分别记录 failed，未尝试的阶段保留 not_run；提案与预批准字段不一致不会确认。

## 范围与已知限制

首批为本机独立后台。两个应用手动启动，需各自 Ctrl-C 后停止存储；没有自动公司服务托管。
Web 配置变更需同步私有配置文件、重启两进程、重新 probe。research 依赖显式 Tavily 配置、
当前 Playbook/国家政策及老板确认。仅有研究信号/假设不能自动晋升已验证需求。
unknown 保留费用事实与并发槽，无自动重发；单会话最多 100 轮；业务解释使用可核验摘录，
来源撤权后保守隐藏后续派生历史。本批不增加通用 Codex 级电脑/代码操作能力。

## 独立审查

待写入最终 fresh-context 审查结论、修复证据和延期小项。

## 执行裁定（完整记录）

- Ruling: use native-managed worktree under ~/.codex/worktrees rather than repo-local .worktrees — installed worktree skill prioritizes native tool and user requested isolation — no behavior cost, artifact paths differ.
- Ruling: test examples that assert private step constants will be replaced by observable no-repeat behaviors — test skill requires behavior tests and spec requires no duplicate external requests — cost is slightly larger test setup.
- Task 2: Ruling: add insufficient_balance classification for HTTP 402 — approved spec explicitly separates balance shortage from authentication/rate limit although plan Literal omitted it — if wrong the UI may mislabel provider errors, tested with controlled HTTP.
- Task 3: Ruling: reserve requires explicit model keyword for auditable provider/model identity; current configuration authorization belongs to Gateway authority, not quota repository — limits and identities remain trusted injection — wrong assembly could apply stale limits, covered at composition.
- Task 3: Ruling: stable tenant/employee bucket locks protect counts derived from persistent invocation ledger, rather than resettable increment-only counters — config changes cannot reset usage and rejected reservations are excluded deterministically — counting cost grows with ledger and is indexed.
- Task 5: Ruling: dispatch intent is persisted as turn.dispatch_state with preallocated Run ID in the turn transaction, not a second table — one row preserves atomicity with less dual-write recovery — no loss of semantics; worker explicitly scans pending intents.
- Task 5: Ruling: replace planned test import of private domains.assistant.models with real delivered-turn/new-input behavior via public service — boundary checker rejects new domain-internal test imports — preserves intended coverage without exposing private state helpers.
- Task 6: Ruling: Run audit remains boss-only; manager/sales cannot query Run details — existing canonical audit service only authorizes boss, and chat must not widen access — fewer role capabilities than plan's generic mapping language.
- Task 6: Ruling: reject nonempty pagination cursor until canonical readers supply stable signed cursors; lists remain explicitly bounded — an invented cursor would imply stable paging absent from domain services — only first bounded page in this batch.
- Task 6: Ruling: conservatively hide all downstream turns when any earlier source becomes inaccessible; cap sessions at 100 turns and fail on oversize context rather than truncate — preserves dependency closure and explicit exclusions without guessing natural-language provenance — may hide more than necessary and require a fresh session.
- Task 7: Ruling: submit_once additionally requires submitted_by and rechecks current active boss in the canonical directive service — relying on earlier chat-role check leaves a revocation window — trusted workflow must pass employee identity, no model actor field.
- Task 7: Ruling: deterministically collect explicit Chinese/English labeled fields, asking clarification for ambiguous prose; also require per-query result limit — arbitrary substring matches cannot prove budget/market intent and the existing query schema requires this limit — more clarification than unrestricted natural language, no silent paid defaults.
- Task 7: Ruling: business explanations use verified source excerpts with dependency closure instead of accepting any paraphrase with a valid ID — a real citation does not validate an invented claim — less fluent summaries until semantic evidence validation exists.
- Task 8: Ruling: add migration 0064 for private validated-result sequence and trusted context-reference checkpoint — workflow audit cannot hold chat content and an in-memory slot cannot recover committed results — one extra migration, conservatively unknown if checkpoint fails.
- Task 8: Ruling: construct typed assistant ports before canonical engine, bind dispatcher afterward — existing engine validates handlers at registration and canonical services already exist — keeps one engine with no mutable handler proxy; factory does not own/start another scheduler.
- Task 9: Ruling: use existing ConfiguredApiDependencies and RequestIdentity injection instead of a parallel assistant router container — same authenticated middleware and runtime dependency lifecycle already expose current identity — fewer standalone router injection seams, no authorization bypass.
- Task 9: Ruling: main router registration automatically feeds the existing zero-I/O OpenAPI exporter; leave exporter unchanged, defer full profile assembly to Task 10 — no duplicate schemas or second API factory — generated Web types still require Task 11 generation.
- Task 10: Ruling: model status is projected by domain service.view rather than a second infra public_model_settings helper — avoids duplicate readiness logic — deployment file must match saved immutable version before both processes restart.
- Task 10: Ruling: add migration 0065 for config head/process/probe and 0066 for invocation owner/lease; recover only under canonical scheduler lock, expired previous owner reserved→rejected/dispatched→unknown — required durable recovery evidence — legacy ownerless calls remain manual.
- Task 11: Ruling: reuse existing exact proposal-confirmation screen via proposal_id; independent session component replaces only old model composer after assistant availability — keeps existing confirmation permissions — initial unavailable state retains legacy interface.
- Task 12: Ruling: research uses existing v2 execute_search max_retries=0; no new flow version because topology/retries unchanged — model failure now returns fixed failed outcome with unknown quota retained — old paid unknown requests remain non-replayable.
- Task 12: Ruling: explicit standalone_research constructor marker permits only research_factory+assistant_factory with local resources; pilot remains closed — legacy constructor correctly rejected new optional research until distinct intent was supplied — adds narrow deployment surface covered by controlled real factory tests.
- Task 12: Ruling: optional research settings remain in private deployment file; actual Tavily/page/S3 resources are owned by an outer async lifecycle — avoids creating/leaking resources before canonical scheduler entry — two configuration files must match the deployment.
- Task 13: Ruling: new immutable assistant/usage integration fixtures use per-test databases — shared legacy migration downgrade tests must start without unrelated durable facts — slightly slower tests, no production deletion policy relaxed.
- Task 13: Ruling: refresh legacy schema-head assertions, include actual handoff handlers, align rolling-window seed time, bypass ambient proxies only in loopback test clients, and stub existing handoff compatibility read only in quotation lifecycle fixture — observed baseline test assumptions were stale — test-only repairs could mask unrelated drift, targeted regressions retained.
- Task 13: Ruling: prepare DeepSeek credentials/SDK before marking a request dispatched, still after Gateway authorization and reservation — missing credentials cannot consume a provider call — preparation Protocol is trusted to perform no HTTP; real connector test verifies quota released. RED invalid→GREEN rejected with next call allowed.
- Task 13: Ruling: extend Linux test image source allowlist for existing handoff and new model composition dependencies — previous explicit image omitted an imported module — source-only offline image grows; no environment/config added.
- Task 13: Ruling: repair existing acceptance helper lint without excluding historical directories — full lint gate otherwise fails on baseline support files — no evidence outputs rewritten, only import/explicit-check/style.
- Task 13: Ruling: conduct fresh whole-branch review while the final complete regression continues, after implementation is committed — independent read-only review does not depend on test completion — acceptance remains in progress until both review fixes and checks finish.
