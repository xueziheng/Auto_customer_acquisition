### Spec Compliance

- ❌ Issues found：撤销权限后的清理不是稳定状态。发件身份管理页与 Smart Inbox 的较早请求能在当前列表已返回 403 后恢复受限内容，违反 Task8 明确要求，见 I1、I2。
- ✅ 文件映射按 Controller 本轮澄清处理：`client.ts`、`api-client-identity.test.ts`、`smart-inbox.test.ts` 没有 hunk，不作为机械缺项。已足够的 identity provider / request scope 可复用，新增 `core-request-generation.test.ts`、`message-evidence.test.ts` 承接行为测试；这不豁免 I1/I2 的行为要求。
- ✅ 审查基点 `31922c4d266260a9016a4641ce4ca8afe04f008e`，源码 `96c0c01ddf42cfbea79fb10c9fa349b79be4f325`。按顺序阅读完整 `review-31922c4..47772bd.diff`（4570 行），未重新生成 Git diff。报告补证另至 `fd550a3c64866e34a3d303beb104e672afbe1fe0`，补证不改变源码。

### Strengths

- 发件身份 API 只映射原域登记/预热命令，actor 与 tenant 来自可信 RequestIdentity；确认值严格要求 JSON boolean true，未开放认证结果、开始日期、强制 active：`apps/api/routers/sending_identities.py:77`、`:127`、`:148`。管理读取保留 Campaign 可用列表含义，并在域内限制 boss/TENANT、逐行授权：`domains/sending_identity/service_impl.py:1698`。
- 新管理 SQL 复用 tenant-bound `_joined()` 并有 SQL LIMIT；具名核查 `_joined()` 的租户条件确认没有丢失隔离：`infra/db/repositories/sending_identities.py:305`、`:433`。
- canonical message 原件下载使用生成 API、blob、no-store，并以 message ID + identity scope 防旧响应；Handoff 原件拒绝主动使整页 gate 失效：`apps/web/src/components/MessageEvidenceDownload.vue:11`、`apps/web/src/views/crm/HandoffQueue.vue:263`。
- 登记未知结果冻结原 payload、重放原耐久幂等操作；预热未知只核对精确 sid，未以相同 target 推断调用成功：`apps/web/tests/identity-registration.test.ts:6`、`:25`。接管精确历史深链与接受后成功提示有回归：`apps/web/tests/handoff-queue.test.ts:845`、`:896`。
- 来源记录与客户/人工确认区分、未知缺项不被当成空事实，且已补 desktop/390 实际填充验收：`apps/web/src/views/crm/HandoffPacketView.vue:42`；浏览器过程与最终版本证据见 `task-8-report.md` 的“填充浏览器门最终验收与视觉修复”及“最终版本与交付”。

### Issues

#### Critical (Must Fix)

- 无。

#### Important (Should Fix)

- **I1 / P1 — 发件身份页在权限拒绝后允许旧通道回填受限复核内容。** `apps/web/src/views/SendingIdentityCenter.vue:33` 的 `protectedFailure()` 只清空 refs，没有使 gate 或相关请求 generation 失效；`:117` 的旧 `reviews` 请求因此仍通过 `op.valid()` 并重新赋值。实际步骤：点击“读取未关联邮件复核”并延迟响应 → 点击“刷新状态”，管理列表返回 403 → 旧 reviews 返回 200。组件同时显示“当前账号无法查看发件身份”和旧复核摘要 `protected-review-summary`。同机制也会影响旧 `exact`/`command` 成功处理；这不是新 HTTP 越权的证明，而是明确违反页面撤权后不得恢复旧受限数据的要求。修复应让拒绝处理原子地清内容并失效相关在途读取/写回，正确收尾 loading/busy，并避免旧 finally 干扰恢复后的新请求；用同身份、跨通道的 deferred 回归覆盖。
- **I2 / P1 — Inbox 列表拒绝没有使在途详情失效，旧原件与下载入口会重新出现。** `apps/web/src/views/inbox/SmartInbox.vue:145` 的失败分支仅清 `items/detail/nextQuestions`，没有更新 `detailVersion` 或使详情/纠正请求失效；`:175` 的旧详情检查仍有效，随后 `:177` 恢复 detail。实际步骤：选择第二条会话并延迟详情 → 刷新列表返回 403 → 延迟详情返回 200。组件再次显示 `second-account`、原件引用 `protected-second` 与“下载邮件原件”，同时保留无权访问提示。修复应在当前列表 401/403 等安全拒绝时同步失效详情、建议及写回并清选中对象，而不只清当前 DOM；补此跨通道回归及旧 error/finally 不回写的新请求断言。

#### Minor (Nice to Have)

- **M1 — 历史验证噪声尚未清零。** `task-8-report.md`“全量 lint”记录 exit0 但仍有 120 warnings，“最终版本与交付”记录 Git stderr 非零字节。报告已明确是既有格式/共享 Git 噪声且捕获不维修；本次不据此扩大生产修复范围。全仓 warning 归属及清理交 Task12，不能描述为“全仓 lint 输出无警告”。

### 定向检查与执行证据

- 具名风险“复用 request scope 是否会自动失效跨通道请求”：只定向读取 `apps/web/src/views/costing-quotes/quote-request-scope.ts`，确认 `begin(channel)` 仅替换同通道请求，身份变化、scope watch 或显式 `invalidate()` 才改变共享 generation。这排除了 I1/I2 被既有 scope 隐式修复的可能。
- 具名风险“新增管理 SQL 是否漏 tenant”：只定向读取 `infra/db/repositories/sending_identities.py` 的 `_joined()` 附近，确认 `SendingIdentityRow.tenant_id == self._tenant_id` 且 domain join 含 tenant。
- 具名风险“提案异步对象切换能否被 UI 锁阻止”：只定向核查 `CommandCenter.vue` 的创建/确认/新建按钮调用点及 disabled 条件，本轮未据此新增未经复现的问题。
- 为定向临时 runner 查既有测试环境，仅核查 `apps/web/vitest.config.ts` 与 `package.json` 的 environment/dependency 字段；没有安装依赖。
- **唯一产品反例运行**：工作目录 `/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`；命令 `node apps/web/node_modules/vitest/vitest.mjs run --config /private/tmp/task8-review-races/vitest.config.mjs --root /private/tmp/task8-review-races`；**exit1，2 tests / 2 failed**。两个失败均为上述受限 DOM 不应重现的断言，实际重新出现的合成标记写在输出中。测试代码和 runner 位于 checkout 外 `/private/tmp/task8-review-races/`，不改变生产文件、仓库测试、索引或 Git 状态。
- 临时 runner 先前 3 轮只发生 harness 启动问题：错误 `vitest/config.js` 绝对入口、未安装 jsdom、`/tmp` 与 `/private/tmp` canonical 路径；它们均没有执行产品测试。改用包导出 `vitest/config`、仓库已有 happy-dom 和 canonical 路径后才运行上述 2 项。没有把启动失败当作产品 RED。
- 未重跑实现者已报告的后端、全 Web、build、schema 或敏感扫描。已读报告新增“冻结后补充验证证据”：34 文件增量敏感扫描零命中；exporter 与 generator 独立 exit0；临时类型文件与现有生成文件逐字节一致。

### Cannot verify

- ⚠️ 仅由本 diff 无法再次证明未变更的全部当前后端权限链、原登记耐久唯一性与预热事务状态机；本审查验证窄 API 的调用方式和管理授权增量，沿用 Task7 已批准边界，未扩大检查全域实现。
- ⚠️ 本人未重新启动 owned 环境或重放填充浏览器链。Controller 已实际查看最终身份、填充 Inbox/Handoff 截图；报告给出原件实际下载、接管 POST 204 与 accepted 历史只读结果。本轮接受这些既有执行证据，不伪称本人独立重做浏览器验收；临时截图长期归档仍由 Task13 完成。
- ⚠️ 最终源码没有“180 后端整组全过”或“最终 359 Web 全量通过”的执行记录。准确证据是后端 179 passed/1 failed 后修复项定向 1 passed，Web 曾全量 358 后增量最终 33 passed 及最终 build。此为已如实披露的验证范围，不能从部分运行合成全组结果。
- ⚠️ 无法从报告摘要逐条确定 120 lint warnings 的文件归属或确认每条均为既有；保留 M1，不重复全量 lint 来重建证据。

### Assessment

**Task quality:** Needs fixes

**Reasoning:** API 复用、tenant 管理读取、人工确认与真实填充验收路径有明确证据；但两个真实组件反例证明权限拒绝后受限内容可被旧请求恢复，当前 Task8 不能通过撤权清理门禁。先修 I1/I2 并用上述跨通道反例验证，避免只重复身份切换场景。


## Fix round 1/5 限定复审

### Finding Verdicts

- **I1：发件身份页在权限拒绝后旧通道回填受限内容 — ADDRESSED。** `apps/web/src/views/SendingIdentityCenter.vue:33` 的安全拒绝分支现同步 `gate.invalidate()`、`reset()` 并收尾 `listLoading`；旧 reviews / exact / command / list 的 success、error、finally 均失去原 generation，不能再恢复受限内容或改变新写锁。绑定与复核错误文案改在 reset 后设置，未被清理吞掉。`apps/web/tests/core-access-revocation.test.ts:18` 保留原复核反例，后续预热 success/error、新写锁、精确身份及拒绝后恢复加载测试核对具体行为。
- **I2：Inbox 列表拒绝后旧详情恢复原件与下载入口 — ADDRESSED。** `apps/web/src/views/inbox/SmartInbox.vue:145` 现对列表 401/403/404 同步失效 gate、调用既有 `clearProtected()` 并结束本次 loading；该清理同时清选中对象、详情/建议/纠正反馈并递增 list/detail 版本。旧详情和旧纠正不能继续写回或刷新原会话。`apps/web/tests/core-access-revocation.test.ts:31` 保留原详情反例并断言账号与下载按钮也不出现；后续纠正 success/error 与详情 error 测试覆盖恢复请求的 loading 不被旧 finally 提前结束。

### New Breakage in the Fix Diff

- 无新增 Critical / Important 问题。同步失效当前 operation 后显式收尾 busy/loading，再写安全提示，避免只补 `invalidate()` 而留下锁死；恢复读取通过新 generation 正常继续。

### Out-of-Scope Observations

- 原 M1 保持非阻塞，未扩大本轮修复范围；没有新增范围外问题。

### 检查与验证范围

- 已按 `re-review-prompt.md` 限定范围顺序读取完整 `review-fd550a3..3ae0f1e.diff` 一次（4 文件、2 commits），未重新生成 diff、未再检查范围外实现、未重跑测试。
- FIX_BASE `fd550a3c64866e34a3d303beb104e672afbe1fe0`；最终源码 `85b3def23f5b15422f274fc7feeeb7521ec9a610`；报告 HEAD `3ae0f1eb9c63a38a25c3c9af20ab49e7d46bf94d`。
- 已核对修复报告末节的逐轮命令及准确输出范围：两个原反例先 RED；定位器修正后基点代码 **9 项 AssertionError RED**，本轮代码 **9 passed**；最后新增 DOM 断言后最终限定作用组 **5 files / 28 passed**。build、3 文件 lint、3 文件敏感扫描、7 组 boundaries 均报告 exit0，测试源码确实覆盖 I1/I2 与旧 success/error/finally 的恢复边界。
- ⚠️ 本复审没有重跑后端、全 Web 或真实浏览器；本次只改请求生命周期与测试，没有 CSS/视觉变更，原浏览器与全量验证范围仍按前文准确保留，不将 28 项局部测试改写成全量结果。

### Verdict

**Fix round:** All findings addressed, no new Critical/Important breakage。

**Spec Compliance:** ✅ Spec compliant（本 Task8 门禁，原 Cannot verify 范围仍保留）。

**Task quality:** Approved。
