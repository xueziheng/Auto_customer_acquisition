# Task 6B3 实施报告

日期：2026-08-25
基线提交：`ce995ad63c3da20472dd8d7706e738569b493aeb`
实现提交：本报告与实现同一提交；精确 SHA 由提交后 handoff 回报（Git 提交无法在自身内容中稳定记录自身 SHA）。

## 结论

Task 6B3 的 PostgreSQL Phase 1 后端受控闭环已完成：Account Discovery 产生的
confirmed hypothesis 通过 tenant-bound Enrollment 来源字段，与实际 outbound delivery、
入站回复、Validated Need、唯一 Opportunity、真实员工 owner 和 pending handoff 串成一条
可重放、可核验的耐久链。闭环只经过公共领域服务和 workflow/application composition，
没有域间直接导入。

真实 provider/model/mail/外部网络状态：**`not_run`**。本任务按 brief 只使用受控本地
account model、联系人 enrichment、邮箱 verification、reply classifier 和 mail transport
fake；没有真实凭证、网络、客户消息、Gmail/Hunter 动作、部署或 push。因此本结果不能解释
为真实 provider 验收或 Phase 1 已具备运营条件。

## 既有覆盖复用

实施前盘点并复用下列既有行为，不在新集成测试中重建领域规则：

- Account Discovery workflow 的任务读取、受控模型输入护栏、账户/contact resolution、
  verification 与 Campaign enrollment。
- Outreach Campaign approval、sender/contact eligibility、send attempt、delivery correlation、
  stop-on-reply、idempotency 与 tenant-bound authorization。
- Conversation inbound ingestion、classification evidence 持久化与 workflow replay。
- Demand signal/hypothesis/Validated Need 的 EvidenceLevel、字段 Provenance 与唯一晋升。
- Opportunity gate/scoring、`tenant_id + need_id` 唯一约束、Employee owner resolution、
  assignment、handoff packet/queue 及 outbox。
- PostgreSQL/Alembic disposable fixture、0036 migration roundtrip、AppleDouble head 校验。

## 修复的真实缺口

1. Enrollment 原来不保存 Account Discovery 的来源 hypothesis；回复侧只能按 account 猜测。
   现在 `source_hypothesis_id` 从 `EnrollCampaignStep` 贯穿 DTO、模型、服务、repository、ORM
   与 0037 migration。它使用 `(tenant_id, source_hypothesis_id)` 复合 FK；Enrollment 的既有
   tenant/idempotency 唯一约束和 Opportunity 的 tenant/need 唯一约束共同保证重放不分叉。
   非 discovery Enrollment 仍可为空，但 reply closed loop 会明确 fail closed。
2. 新增生产 `TenantBoundReplyBusinessFactsReader`。它按 typed context 精确验证
   inbound message → outbound deterministic Message-ID → delivery target → enrollment →
   account/contact/sending identity → hypothesis → current need → opportunity；任何缺失、陈旧、
   歧义或跨租户事实都拒绝。返回值只含有界业务 metadata，不读取/返回消息正文或凭证。
3. Demand 公共服务增加客户回复 EvidenceLevel 追加接口，并在 hypothesis view 暴露其明确
   `validated_need_id`；Opportunity 公共服务增加 tenant-bound `get_by_need`，避免 application
   层直接访问 repository 或做跨域 SQL projection。
4. 新增 `DurableReplyOpportunityIntake`。首次 reply 晋升后，它重新读取 hypothesis、need、
   account、verified contact 与 active Playbook，逐字段验证 reply quote provenance；金额仅用
   `Money.multiply(Decimal)` 推导，缺字段/门槛失败时不伪造 Opportunity。通过 gate 后调用
   EmployeeService 解析真实 owner，再用 OpportunityService 分配。重放会复用唯一 need 对应的
   Opportunity，也能补分配已存在但未分配的机会。
5. `ComposedReplyActionPorts` 现在先追加客户证据、晋升 need、创建并分配 opportunity，随后
   handoff 才消费该机会。`target_price` 只接受确定性的 `{amount, currency}` JSON 字符串，
   不引入 float 或隐藏默认业务数字。
6. 新增真实 PostgreSQL closed-loop acceptance，覆盖发现、联系验证、批准 Campaign、受控
   send/correlation、stored/classified reply、Validated Need、assigned Opportunity、完整 pending
   handoff，并断言 replay、tenant isolation、stale-link rejection、provenance、stop-on-reply 及
   workflow/outbox/log 中无 reply body、邮箱、credential marker 泄漏。

## TDD 证据

每项生产修改均先由新增行为测试见证失败。主要 RED 如下：

1. Enrollment source contract：
   `pytest tests/unit/test_outreach_enrollment_service.py::test_enroll_preserves_exact_source_hypothesis_and_binds_it_to_idempotency -q`
   得到 `TypeError: unexpected keyword argument 'source_hypothesis_id'`。
2. production business facts adapter：
   `pytest tests/unit/test_reply_business_facts_adapter.py -q`
   得到 3 failures / module missing。
3. 首次晋升的 opportunity composition：
   `pytest tests/unit/test_composed_reply_actions.py::test_first_promotion_creates_and_assigns_opportunity_before_handoff -q`
   得到构造器不接受 `opportunity_intake` 的 `TypeError`。
4. 已存在未分配 Opportunity 的 replay：
   `pytest tests/unit/test_reply_opportunity_intake.py -q`
   得到 owner conflict `ValidationError`，证明重放不能补齐 assignment。
5. migration head：head 文件测试仍期望 0036，新增 0037 后失败；0037 PostgreSQL roundtrip
   首次因测试错误比较 SQLAlchemy type object 而失败，修正为数据库反射字符串后才验证真实
   schema/ORM parity。
6. closed-loop acceptance 首次因 Campaign approval 时间与受控 clock 不一致 fail closed；
   修正测试夹具后转绿，没有放宽 Outreach 生产校验。
7. 精确 outbound 链补强：
   `test_reader_rejects_outbound_message_not_bound_to_exact_enrollment` 见证
   `DID NOT RAISE`；`test_action_rejects_message_whose_stored_outbound_link_is_stale`
   见证 evidence snapshot 缺 `outbound_message_id`。补齐 inbound stored link 与 delivery
   correlation 校验后两项转绿。

## GREEN 与门禁

- 精确 outbound/composer/business adapter 聚焦回归：`23 passed`。
- 最终受影响 unit 选择集：`90 passed in 2.56s`。
- 最终 PostgreSQL integration 选择集（含 closed loop、reply workflow、Outreach lifecycle、
  0036/0037 roundtrip）：`16 passed in 16.76s`。
- closed-loop 单项在精确 outbound 约束后复验：`1 passed in 10.83s`。
- migration from empty：
  `test_roundtrip_downgrade_base_then_upgrade_head`，`1 passed in 13.18s`；执行
  head → base → head，并由 `finally` 再恢复 head。
- 完整 Alembic PostgreSQL integration 文件：`58 passed in 100.91s`，覆盖所有历史
  revision roundtrip、约束与 0037 schema/ORM parity。
- upgrade from previous head：0037 roundtrip 执行 `0037 → 0036 → head`，验证 nullable
  `VARCHAR(40)`、tenant 复合 FK、tenant-first index 与 ORM columns 完全一致；包含在上述
  16 项 integration GREEN 中。
- Ruff（全部 changed Python）：`All checks passed!`。
- configured mypy：`Success: no issues found in 357 source files`。
- `scripts/check_boundaries.py`：七项全部通过。
- `scripts/scan_sensitive.py` working tree：exit 0。
- `git diff --check`：exit 0。

## 剩余风险

1. 生产进程仍需在部署 composition root 中实例化新 business reader/opportunity intake 及其
   actors；本任务证明 adapter 和公共服务闭环，不包含部署或真实 worker 启动验收。
2. 真实 provider/model/mail acceptance 仍为 `not_run`；受控 fake 不能证明供应商 SLA、
   模型质量、真实邮箱 threading 或 provider correlation 的线上兼容性。
3. 0037 上线前仍需按运维流程备份并在目标环境演练；这里只证明 disposable PostgreSQL 的
   空库与 previous-head 往返。
4. frontend/browser 闭环属于 Task 6C，完整仓库/合并 HEAD 总门禁属于 6D；本任务不提前声称
   两者完成。

预存未跟踪的 `apps/web/node_modules` 未纳入测试报告、暂存区或提交。
