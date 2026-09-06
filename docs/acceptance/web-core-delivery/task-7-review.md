# Task7 独立规格与质量审查

## Spec Compliance

**Spec verdict：✅ Spec compliant。** 本批要求的服务、Repository、同事务事实端口、API、下一问整链、生成类型与指定测试文件均有对应变更；未发现 Missing、Extra 或 Misunderstood 的阻断项。审查范围为 `97a534796944dd891f908c163d0a1cd2f88050e1..1aa1d99c21339e6a4197c18f655da607bf2fa2ea`，源码提交 `3090ee35763b6e6f84b302d4a91715e18cb99bfc`。

## Strengths

- `domains/conversations/inbox_access.py:60`、`:82`：受信 snapshot 仅为上界；严格角色/scope 配对、当前 active 与角色一致、manager 只到活跃直属，未知 owner 不向员工开放。`apps/api/identity.py:49` 将原受信员工及 owner 范围映射为独立 InboxActor，未替换通知自身的 actor。
- `infra/db/inbox_access.py:96`、`infra/db/repositories/conversations.py:421`：当前主体、owner、直属和 snapshot 用单 SQL 谓词求交，列表过滤先于 LIMIT。`domains/conversations/service_impl.py:786` 对输出资源作最后单语句重验；`tests/integration/test_inbox_access.py:145` 和 `:697` 分别覆盖越权窗口占满、READ COMMITTED 旧主体与新归属拼接反例。
- `infra/db/inbox_access.py:70`、`domains/conversations/service_impl.py:542`、`:800`：纠正从真实 Message 解析 account，在同 UoW 内先锁 ownership、再按 ID 排序锁 actor/owner，锁后重验，corrected_by 必须等于 actor。`tests/integration/test_inbox_access.py:271`、`:511` 用独立事务及第三连接观察 PG 阻塞，覆盖撤权前后两种提交顺序；既有原判、不重放事件和纠正幂等断言保留。
- `tool_gateway/handlers/inbox_evidence.py:130`、`:167`、`apps/api/routers/inbox.py:190`：独立 LOW/FREE/NONE 插件，不修改核心管线；Message 解析原件，校验 tenant/kind/MIME/size/hash，task 绑定一次性交接，返回前再核当前权限与原引用。HTTP 拒绝额外 body/query，安全附件响应；`tests/integration/test_inbox_access.py:361`、`:415` 覆盖对象读中转移、错类型/租户及旧链接拒绝。ADR0028 第 11 行如实限定合法返回后不可撤回，无附件或长时公链扩展。
- `workflows/reply_qualification/questions.py:48`、`:74`：下一问纳入第五动作，Conversations 先授权，Outreach 使用真实 manager/sales 与精确 account/Enrollment 范围，Demand 保持原公共签名并核对 account 和所选 Message 证据，成功及 need_unavailable 返回都再核当前范围。`tests/integration/test_reply_completion.py:256` 的真实 Need 链覆盖 staff HTTP 成功和中途转移拒绝。`domains/outreach/permissions.py:380` 附近新增门槛把 account-only SELF 限于 REPLY_SOURCE_READ，未开放原反馈或写动作。
- `docs/adr/0028-inbox-current-ownership-access.md:5`、`docs/superpowers/specs/2026-09-06-inbox-access-7.md:7`：明确 infra 的最小事实投影与同 session 边界；没有会话域导入员工内部实现、owner 缓存列、新迁移或新发送路径。

## Issues

### Critical

无。

### Important

无。

### Minor

- **M1：纠正接口的异常说明未随新权限契约更新。** `domains/conversations/service_impl.py:555` 仍写“未分类 / 跨租户不可见 → ValidationError(消息尚未分类)”，但当前跨租户/无权/不存在 Message 先由 `:800` 的访问校验统一抛 PermissionDenied；仅授权成功但未分类才是 ValidationError。该说明会误导后续调用方或维护者。请区分这两类情形，并同步 `tests/integration/test_conversations_correction.py` 中对应跨租户测试的旧 docstring。实际拒绝行为与本批规格一致，此项不阻断。

## Cannot verify 与验证边界

- ⚠️ **实际执行记录与资源清理未在本次重新运行。** `task-7-report.md:70` 记录同版本 179 passed、无 skip；`:84` 记录 Ruff/Mypy/结构自检与扫描成功；`:93` 记录独立 exporter/generator、TS 成功；`:95` 记录 owned PG/MinIO 清理。可见报告完整，测试源码与所述行为相符，未将未亲自见证命令输出解释为证据不存在；控制器应保留这些既有执行证据。本审查未重跑测试、静态检查或 schema 生成。
- ⚠️ **未改动的员工归属写路径与身份获取全实现不在本 diff 中。** 本次依据 `task-7-review-context.md` 的已核 transfer/锁序裁定、diff 内真实公开 transfer 调用及双向 PG 竞争测试，未宣称重新完成员工域整体审计。控制器已给定该裁定，不需要为本次复跑同版本验证。
- ⚠️ **旧 technical review/retry、qualify、quotation source、通知与 CRM 完整实现未扩读。** 本 diff 未修改这些 helper/路由，新增路径使用独立 actor/action/plugin；既有权限回归列在最终 179 项命令中。此结论只表示本批没有可见的顺带放宽，不是全分支无漏洞证明。
- ⚠️ **浏览器页面、真实 Gmail/模型/客户发送、生产部署、独立附件能力及全仓门禁未验收。** 这些由 brief 明确排除或归后续 Task8/Task12，不能从本次 Approved 推断已经交付。

## 已执行审查

读取 brief、完整 report、控制器 review-context、全局与相关目录规则及 task-reviewer-prompt。完整 review package 按顺序有界 chunks 审查一次，随后仅机械提取已审条目的行号；未重新生成 diff，未补读生产源文件，未开展域外定向扩查或运行 Git。无源代码 hunk 截断补读需要。仅写本报告，无生产、索引、HEAD、分支或 shared.git 修改，无子代理、凭证读取、测试重跑或外部动作。

报告中的 AppleDouble Git stderr 是 `task-7-report.md:106` 已登记的既有环境噪声，未被当作新增产品问题或静默修复；同版本测试汇总没有报告 warning/skip。

## Assessment

**Task quality verdict：Approved。** 权限矩阵同时落实到领域与 SQL，纠正写入采用真实事务锁，原件和下一问均保留最终当前范围复核；测试直接验证拒绝与竞态行为。仅一项接口说明过时的 Minor，不影响本批安全行为与规格完成度。
