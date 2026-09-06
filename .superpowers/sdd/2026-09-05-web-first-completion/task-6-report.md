# Task6实施报告：完整回复、下一问与受控站内通知

状态：DONE_WITH_CONCERNS（约定范围已完成；范围限制见末尾）。控制器尚未独立审查。

工作树：`/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion`；分支`codex/web-core-completion`。
基点：`21b0b770fee078575c437d57a5f1b01bc8f5c689`。
源码及正式spec/ADR提交：`6d06a7b34db3f76ee3e6ee65c856004e44fba371`（63文件）。以下最终测试都在与该提交完全一致的源码执行；测试之后没有生产/测试源码变更。
本报告写入前最后HEAD同上；报告自身另作纯文档提交，其最终完整HEAD在交付消息列出（不能把文档自身散列内嵌到本文件）。

## 实际范围与公开契约

正式规格：`docs/superpowers/specs/2026-09-06-reply-completion-6.md`；决策：`docs/adr/0027-reply-composition-and-controlled-notifications.md`。对应就近AGENTS已记录受控通知与窄读action；根九条硬边界未变。

1. 原Scheduler完整ReplyQualificationComposition已装配；controlled入口实际提供reply_factory。原InboundMessageStored消费者注册后，同一5b入站driver自然enabled。资源通过ReplyRuntimeResources传本进程sessions、BoundedRawArtifactStore、canonical Opportunity与时钟；Inbound公开bounded端口和原technical-review wrapper分离。reply借用原inbound持有的objects，不创建第二engine、不关闭borrowed资源，原finally负责失败清理。
2. 原canonical Demand构造注入一次性verifier委托；原Outreach形成后绑定真实TenantBoundCustomerReplyEvidenceVerifier，消费/ready前完成。未绑定使用、再次绑定（包括同对象）拒绝；每runtime独立，无global、私有字段修改或第二Demand。真实高意向走此原Demand证据门。
3. reader仅get_bounded，核tenant/message/artifact metadata，复用5a parse_inbound_content。完整subject+raw HTML guard_body与subject+实际文本均在预算/投影前guard；超限拒绝，不截断或回退无界get。普通邮箱/URL签名投影替换，真实credential marker提前拒绝。原body/subject/当前连续片段仅内存与原Raw保留，不进入模型或workflow state/event/log。
4. 当前片段排除明确plain引用行及历史分隔后缀、HTML blockquote/gmail_quote/yahoo_quoted/divRplyFwdMsg；不删除引用后拼接新句。最多200片段，异常或超限不可验证。模型仅见固定subject `(current reply)` 和当前片段的安全投影，分隔符不可作证据。最终quote在任何分类持久写和动作前核完整可靠原文及单一当前片段，并拒投影占位。QualificationAgent保留独立过滤语义，但parser确定性标记实际被丢弃候选，新workflow遇该标志整体拒绝，不能静默改成缺项。模型JSON不能自报该内部标志。
5. 真实部分Need+确定性missing_for_sourcing+无Opportunity，原CREATE_FOLLOW_UP以自身消息稳定幂等键入pending；不称draft/sent/handoff。后续回复可续补同一Need；重复product_category严格同原值则保留原Provenance并从原mutable更新口去除，不同值安全失败，既不另造Need，也不假称已排人工接管。所有候选先过原文门，类别同值也不能绕过。
6. 新只读GET `/inbox/conversations/{conversation_id}/messages/{message_id}/next-questions`，无query/body，额外query拒绝400。可信当前员工先经Conversations `require_reply_internal_access(action="next_questions")`，本批active boss-only。精确链：Conversation指定入站Message→原SENT双键关联→Enrollment.source_hypothesis_id→Hypothesis.validated_need_id→Need；target.account必须等于Conversation.account，且Need至少一个现存字段source_ref等于选中Message。无法精确关联返回need_unavailable，不按account选最新Need。
7. DTO `ReplyNextQuestionsView`：conversation_id/source_message_id、need_id或None、state（suggested/need_unavailable/no_missing_fields）、实际completeness或None、topics/suggestions各至多2项。缺项和完整度只从真实Need读取，调用原suggest_next_questions，固定英文建议不含自动价格/交期承诺；API类型由原OpenAPI生成。
8. Outreach新增独立公开 `resolve_reply_source` / REPLY_SOURCE_READ，当前boss-only，复用原完整SENT/双key/Enrollment校验，资源授权显式实际campaign/account/enrollment。原resolve_delivery_feedback仍SYSTEM+精确identity，不放宽。没有按任意Message-ID读关联的独立HTTP接口。Task7须把该action和后续Enrollment/Demand读取纳入真实当前owner矩阵，不能伪造boss。
9. canonical通知受众使用Opportunity窄 `get_notification_audience_target` / NOTIFICATION_AUDIENCE_READ，仅SYSTEM且单一精确notification_opportunity_id，仅返回account_id；再Employee窄 `get_notification_owner` / NOTIFICATION_OWNER_READ，仅SYSTEM且Actor.notification_account_id精确tenant/account，返回当前owner或None。原Opportunity.get及Employee.get_ownership仍拒SYSTEM；原list_active过滤停用owner/manager，无旧事件assigned_to fallback。
10. 专用 `apps.notification_worker.controlled` typed controlled_in_app入口真实claim→原模板→dedup→router→InApp持久送达。仅注册/选择in_app，NORMAL/URGENT和模板/受众语义沿原代码；完成只代表显式受控站内通道。生产默认仍缺email即拒绝、保持原双渠道，不注册永远失败的假email或增加普通降级环境开关。
11. 原Supervisor管理第四独立notification进程、loopback端口与同owner network/health/stop/restart。安全状态有notification_url；该端口 `/health/ready`、`/health/capabilities` 返回mode=controlled_in_app、in_app enabled、email disabled。同owner进程真实启用inbound并同端口restart，不是只改health字符串。脚本内部导入统一scripts包路径，Mypy采用explicit-package-bases。
12. health在已确认listening时先5秒软等待正常退出；启动前取消立即回收。真实已bind但runtime未ready窗口由health serve异常/取消路径显式shutdown owned uvicorn，保留primary。此health Config的连接/task graceful等待显式5秒。ASGI lifespan和数据库dispose没有新造硬超时，所以不宣称整体cleanup硬上限5秒；真实窗口检查同端口可重绑、无新增残留异步任务。

## 核心因果验收

`tests/integration/test_reply_completion.py` 全部从真实5b入口、实际owned PG/MinIO/受控Provider执行。仅外部模型与Provider响应受控；无seed SENT/Message/Need/Opportunity/审批结果。prepare_sent只增加可选reply_source=True，经原公开Demand/Enrollment绑定真实source_hypothesis_id，真实员工输入使用employee_input Provenance；真实Playbook独立审批、ownership及原机会门槛保持。5b helper默认路径单项回归通过。

- 真退订、自动回复、拒绝、clear_interest：原Gateway→Raw→原Outbox→原engine→Agent→原Conversations/Outreach动作；正确stop/不stop。
- 真缺资料→原证据verifier→Need+pending无handoff；受权boss读取真实缺项的最多两条英文建议。补齐后重建runtime并沿同一binding cursor继续，重复第一封和新回复只保留一个Need，真正Opportunity/Handoff；类别变更反例零覆盖/零误接管。
- 完整采购回复生成真实HandoffRequested；其canonical通知job经source_job_id精确join recipient/channel，实际InApp已投递。原另一个真实SLA urgent通知保留，未删除或关闭；不能把它混入本次HandoffRequested唯一性计数。当前owner转移使用原Employee公开流程，受众重新读取，无旧assigned_to fallback；停用owner的单元反例同样拒绝fallback。
- 普通签名允许分类且原话摘录真实；模型捕获仅subject/body且无locator。plain历史、blockquote、gmail_quote、删除引用区跨段拼句、生成占位、fabricated quote六类在原Agent→Step前门拒绝，持久分类/Need/Opportunity/Handoff零写。普通当前回复+历史退订不能造成抑制。
- 真实credential marker由原5a Gateway更早转credential_marker review，零Message、零分类、零reply Run、模型零调用；reader独立完整HTML/实际文本及尾部预算guard反例也通过。不能为了让reader收到secret而绕过5a。
- 原engine取消真实入站Run，重建runtime保持cursor，零classifier调用、单一Message。未知受控发送先真实记录provider send调用再抛may_have_written错误，原状态非SENT/零Message；租约到期后重建公开依赖并经原发送入口搜索恢复，send持久计数恰好1，search至少2，最终SENT；没有重发当恢复。
- 原文original_body/evidence_segments不在workflow或outbox序列化输出。Handoff evidence从真正当前片段取有界原话，不向模型传原件。

## 最终验证：源码6d06a7b34db3f76ee3e6ee65c856004e44fba371

所有pytest的命令前缀均为 `env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest`；工作目录为本报告工作树。未读.env/既有DSN/secret/token/cookie。使用已有私有Python3.12、Node24.15.0与private Web node_modules；fresh依赖声明复验沿Task4，完整环境复核归Task12。

### 最终主组合

精确命令：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/unit/test_reply_content_boundary.py tests/unit/test_reply_current_evidence.py tests/unit/test_reply_runtime_binding.py tests/unit/test_controlled_reply_model.py tests/unit/test_qualification_agent_boundary.py tests/unit/test_qualification_model_port.py tests/unit/test_composed_reply_actions.py tests/unit/test_reply_action_dispatch.py tests/unit/test_reply_customer_evidence_adapter.py tests/unit/test_reply_business_facts_adapter.py tests/unit/test_reply_opportunity_intake.py tests/unit/test_outreach_delivery_feedback.py tests/unit/test_outreach_permissions.py tests/unit/test_notification_worker.py tests/unit/test_notification_templates.py tests/unit/test_notification_router.py tests/unit/test_notification_recipients.py tests/unit/test_notification_projection.py tests/unit/test_notification_contracts.py tests/unit/test_reply_evals_integrity.py tests/evals/test_reply_evals_runner.py tests/integration/test_reply_completion.py tests/integration/test_controlled_notification_delivery.py tests/integration/test_message_content_reader.py tests/integration/test_web_core_launcher.py::test_stack_ready_restart_and_term_owned_cleanup tests/integration/test_email_inbound_page.py::test_real_sent_reply_ingests_message
```

exit **0**，输出 **364 passed in 60.25s (0:01:00)**。没有skip、没有真实Provider。160条原业务eval corpus由原runner消费，smoke零错误机制通过；原prompt文本/模型词表及expected未改，不声称真实模型准确率达标。

### 同源码原权限/服务定向回归

前轮Outreach新增action枚举暴露后，额外检查其它两个被修改权限域，未重复整仓：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/unit/test_opportunities_permissions.py tests/unit/test_opportunities_service.py tests/unit/test_employees_service.py
```

exit **0**，**119 passed in 0.34s**。它是独立作用组，不与历史中间数字相加称另一个主组合。

### 最终静态、生成与类型

- Ruff：`.venv/bin/python -m ruff check <下列53个py作用文件>`；exit0，`All checks passed!`。
- Mypy生产：`.venv/bin/python -m mypy <下列去掉tests/和scripts/的38个py文件>`；exit0，`Success: no issues found in 38 source files`。
- Mypy脚本：`.venv/bin/python -m mypy --explicit-package-bases scripts/controlled_web_supervisor.py scripts/run_web_core_controlled.py`；exit0，2 source files无问题。
- `.venv/bin/python scripts/check_boundaries.py`；exit0，分层/金额float/置信度/事件注册/tenant/AGENTS/域结构七项通过。
- `.venv/bin/python scripts/scan_sensitive.py <BASE至源码提交全部63个修改/新增文件>`；exit0，无命中，无stderr。未弱化scanner；不代表全仓旧fixture零问题。
- `git diff --check`；exit0，无stdout；stderr安全捕获48行AppleDouble环境噪声，未修共享.git。
- 原schema exporter：`.venv/bin/python apps/web/scripts/export_openapi.py > /tmp/task6-openapi.json`；独立exit0。
- 原generator：`node apps/web/node_modules/openapi-typescript/bin/cli.js /tmp/task6-openapi.json -o apps/web/src/api/api.d.ts`；独立exit0。之后未改API契约。
- Web：`npm --prefix apps/web run typecheck`；独立exit0。生成文件在源码提交内，无手写重复DTO。

53个Python静态作用文件（63文件敏感scope另含生成api.d.ts、7个就近AGENTS及2个正式spec/ADR）：

```text
agent_runtime/qualification_agent/agent.py
agent_runtime/qualification_agent/openai_port.py
agent_runtime/qualification_agent/questions.py
apps/api/composition/runtime.py
apps/api/dependencies.py
apps/api/routers/inbox.py
apps/composition_support/email_inbound.py
apps/notification_worker/controlled.py
apps/notification_worker/health.py
apps/notification_worker/runtime.py
apps/scheduler_worker/adapters/message_content_reader.py
apps/scheduler_worker/bootstrap.py
apps/scheduler_worker/catalog_product_runtime.py
apps/scheduler_worker/controlled.py
apps/scheduler_worker/reply_actions.py
apps/scheduler_worker/reply_binding.py
apps/scheduler_worker/reply_composition.py
apps/scheduler_worker/runtime.py
connectors/gmail/inbound_mime.py
domains/conversations/schemas.py
domains/conversations/service.py
domains/conversations/source_access.py
domains/employees/permissions.py
domains/employees/service.py
domains/employees/service_impl.py
domains/opportunities/permissions.py
domains/opportunities/schemas.py
domains/opportunities/service.py
domains/opportunities/service_impl.py
domains/outreach/permissions.py
domains/outreach/service.py
domains/outreach/service_impl.py
infra/controlled/config.py
infra/controlled/reply_model.py
scripts/controlled_web_supervisor.py
scripts/run_web_core_controlled.py
shared/schemas/email_inbound.py
tests/integration/test_controlled_notification_delivery.py
tests/integration/test_email_inbound_page.py
tests/integration/test_message_content_reader.py
tests/integration/test_reply_completion.py
tests/integration/test_web_core_launcher.py
tests/unit/test_composed_reply_actions.py
tests/unit/test_controlled_reply_model.py
tests/unit/test_outreach_delivery_feedback.py
tests/unit/test_outreach_permissions.py
tests/unit/test_qualification_model_port.py
tests/unit/test_reply_content_boundary.py
tests/unit/test_reply_current_evidence.py
tests/unit/test_reply_runtime_binding.py
workflows/reply_qualification/ports.py
workflows/reply_qualification/questions.py
workflows/reply_qualification/steps.py
```

## RED/GREEN与失败归因（不累计为最终通过数）

最终主组合首轮使用上面完全相同命令，但源码为提交前R1（当时尚无bind-before-ready窗口新增测试），exit1，**6 failed, 357 passed in 62.84s**：

- 四个Outreach role矩阵失败：新增REPLY_SOURCE_READ未进入旧测试的完整枚举。只加入boss允许集合；manager/system/sales仍拒绝，资源约束保持。
- 一个产品回归：health尚未listening的取消也进入5秒软等待，违反原0.15秒迅速取消断言。改成只有runtime已确认listening才正常等待，原cancel/Gmail/engine回收保持。
- 一个测试口径错误：secret已由5a Gateway更早归credential_marker review，原本却期待failed workflow Run。修成真实review+零Message/分类/Run，不修改产品提前拒绝行为。

R2定向修复命令：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/unit/test_outreach_permissions.py tests/unit/test_notification_worker.py tests/integration/test_reply_completion.py::test_raw_secret_is_rejected_before_model_at_real_inbound_entry tests/integration/test_controlled_notification_delivery.py tests/integration/test_web_core_launcher.py::test_stack_ready_restart_and_term_owned_cleanup
```

exit0，**45 passed in 27.12s**；该中间版还未覆盖已bind、未ready窗口。

R2新增真实窗口RED精确命令：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/integration/test_controlled_notification_delivery.py::test_cancel_after_socket_bind_before_runtime_ready_reclaims_listener
```

exit1，**1 failed in 6.04s**；真实socket重绑抛`port_in_use`。RED也由测试finally显式关闭本测试owned server，避免留下监听。原因是本机uvicorn.serve异常/取消不自动shutdown，不能用runtime ready标记代表socket未建立。

R3显式owned shutdown后的GREEN：

```sh
env -u TEST_DATABASE_URL PYTHON_DOTENV_DISABLED=1 .venv/bin/python -m pytest -q tests/integration/test_controlled_notification_delivery.py tests/unit/test_notification_worker.py tests/integration/test_web_core_launcher.py::test_stack_ready_restart_and_term_owned_cleanup
```

exit0，**23 passed in 19.69s**；包括原迅速取消、真实窗口同端口可重绑/零新增残任务、正常同端口restart。后续只做import空行Ruff修正及spec/ADR补记，最终主组合364项在相同最终源码再次通过。

早期TDD定位另覆盖下列真实产品缺口：BoundedRaw reader原契约/完整HTML和尾部guard；原Agent固定prompt末尾换行被StructuredReplyModelPort拒绝；缺项Need无法handoff；真实通知SYSTEM读Opportunity.get、随后Employee.get_ownership均被原权限拒绝；原Agent丢非法quote；重复类别被原Demand不可变字段拒绝。各修复有对应持续保留的测试进入最终组。早期分步命令输出未逐字保留为独立归档，故这里仅记录定位、不补造命令或累计通过数；可审计的本报告RED/GREEN精确命令以以上实际最终迭代为准。

另有明确测试前置/断言错误，不能混称产品RED：reply_source前置缺少真实Hypothesis、完整SENT helper断言需真实二步序列；通知原SLA另生成urgent而总数断言混算；未知发送立即恢复仍处原lease需真实时钟推进，之后漏import timedelta；原reader旧fixture缺tenant/account；next-questions拒绝额外query实际为400非422。均只修真实前置或真实口径，不seed终态、删通知、改期望掩盖业务规则。

## 环境、owned清理与工作目录纠正

- 主组合所有PG/MinIO由原Supervisor owned fixture启动；fixture finally断言cleanup_errors为空、owned config删除。四进程真实restart/TERM测试核同owner进程、容器、文件清理；没有pkill/prune或触碰其它owner。受控SQLite在tmp_path，数据库engine/模型lifecycle/objects都由各原owner关闭；未知发送恢复新依赖即使断言失败也finally关闭。临时schema/static文件仅本批`/tmp/task6-*`，不含现有凭证。
- 没有新增迁移，也未手删/迁移共享.git sidecar。T7 Git diff/add/commit会输出AppleDouble相关stderr，均capture不直接回显；命令exit0。源码commit为644行stderr噪声，status仍exit0且stdout空；这是环境噪声而非“底层零告警”。没有repair/repack/delete共享.git。
- 一次早期shell默认python3写入中文失败（环境解释器问题），改用已批准私有.venv Python3.12。`.venv/bin/ruff`不存在，采用同runtime `python -m ruff`。脚本单文件Mypy曾以包/非包两名识别同源而exit2；统一真实导入路径并用explicit-package-bases后exit0。没有读.env寻找环境补救。
- 曾有一次遗漏exec显式workdir，使自己的新测试追加到了原目录 `/Volumes/T7/Company/Auto_customer_acquisition/tests/integration/test_reply_completion.py`。发现后检查该文件完整内容仅为本次刚追加测试（从本次decorator开始，无任何原有内容），原目录此前无该文件；把这段自己的内容转入本工作树对应新文件，再只删除这一个本次新建原目录文件。没有覆盖或删除原既有文件。控制器已获知并裁定该纠正可接受；之后所有exec显式workdir，不再为此访问或修改原目录。数次早期read-only命令也曾因省略workdir读到原目录，未读取凭证或产生写入；不把这些读取冒称工作树验证。

## not_run、限制与后续交接

- 未跑真实模型/真实Gmail/事务邮件/真实客户发送，也未验证真实多渠道完成；受控InApp送达不等于生产邮件已启用。160条eval为受控关键词smoke机制，不是生产模型效果验收。
- 浏览器Message原件授权和完整建议UI归Task7/8；本批只有真实后端整链和当前boss受权GET，未借technical-review Raw口演示Message原件。Task7需把Conversations/Outreach只读action及Enrollment/Demand同当前owner scope打通；Task8用已生成DTO和notification_url能力读取。
- 当前表达识别仅支持spec明确的历史引用格式；未宣称所有客户端历史都识别。无法归属/超限安全失败不代表已自动创建人工任务。主题独有采购/退订信息不进入模型，需原件人工核对。
- next-questions产出是英文建议，follow-up仍pending；没有草稿落库或发送完成承诺。immutable类别冲突是安全失败供核对，不是“已排人工接管”。
- 未重复所有旧46组/整仓/全部Web测试。旧独立testcontainers组（包括旧scheduler reply trigger等）未全量迁移运行；本批真实5b入口owned组合已覆盖所列业务因果链，原reader组已迁有界owned fixture。全仓同版本门禁、已知4处旧敏感fixture形态命中和172旧Web lint warnings按控制器归Task12；scanner未降级。
- fresh声明依赖完整复验沿Task4既有证据、本批沿用批准私有runtime；最终全环境复核归Task12。
- 未改控制器progress、整体plan checkbox、其它brief；未派子代理或替控制器review。所有提交仅本地，无push/merge/deploy。
