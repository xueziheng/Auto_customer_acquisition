# 待接管负责人周期提醒实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** 实现显式每两小时提醒负责员工、接受后停止的完整本机 Web 后端链路。

**Architecture:** 在现有接管流程增加明确的负责人提醒模式，复用持久化定时器与通知管线。兼容旧升级模式，接受事实同时约束产生和投递提醒。

**Tech Stack:** Python 3.12、Pydantic、PostgreSQL、SQLAlchemy、既有 Workflow 与站内通知。

**Spec:** docs/superpowers/specs/2026-09-08-owner-handoff-reminders-design.md

## Global Constraints

- 只提醒当前负责员工；不因超时改变负责人，不通知经理或老板。
- 提醒周期由操作者显式提供；本用户确认的值是 7200 秒，不作为隐藏全局默认。
- 仅对尚未接受的待接管事项提醒；员工接受提交后，后续扫描和通知投递不能再产生该事项的新提醒。历史已投递通知可以保留。
- 初始请求通知保留，重复提醒按持久轮次去重，进程重启和重复事件不造成同轮重复。
- 复用 Postgres Workflow、durable Outbox、通知任务和站内通知出口；不创建 Codex 自动化，不发真实邮件。
- 所有查询保持 tenant 过滤；当前员工/接管状态从服务端事实读取；通知层不复制领域业务判断。
- 不输出真实或合成凭证、Cookie、DSN、私有配置、原始失败材料；测试材料仅留在进程内。
- 只改现有隔离工作目录，不推送、不合并、不删除历史 output，不修复共享 Git。

### Task 1: 负责人提醒完整链路、兼容与验收

**Files:**
- Modify: `workflows/human_handoff/flow.py` 与 `AGENTS.md`：新增明确模式，接受停止与持久重复提醒。
- Modify: `infra/pilot/config.py`、`apps/api/runtime_config.py`、`apps/api/runtime.py`、`apps/api/pilot.py`、`apps/api/composition/runtime.py`、`apps/scheduler_worker/config.py`、`apps/scheduler_worker/runtime.py`：显式周期贯穿配置与装配。
- Modify: `apps/scheduler_worker/notification_projection.py`、`notification_gateway/templates.py`：新负责人提醒原因与准确文案。
- Create: `apps/composition_support/handoff_notifications.py`：API/scheduler共享持久通知任务的机械适配；scheduler保留兼容导出，不共享运行对象。
- 按具体并发证据扩展：`domains/opportunities/service.py`、`service_impl.py`、`permissions.py`、`repository.py`、`infra/db/repositories/` 下现有接管/通知仓储、`apps/notification_worker/` 与通知投递装配。只允许最窄的当前接管事实/投递边界，不复制域规则，不重构通用 Workflow engine。
- Test: `tests/unit/test_human_handoff_flow.py`、对应 runtime_config/pilot 配置测试、`tests/integration/test_human_handoff_workflow.py`、新增 `tests/integration/test_owner_handoff_reminders.py`。
- Docs: `docs/operations/web-internal-pilot.md`、`docs/operations/web-pilot-policy-decisions-2026-09-08.md`；若改变公共契约或需要明确持久版本约束，使用下一个空 ADR 编号记录。

**Interfaces:**
- Consumes: `build_human_handoff_definition(t1: timedelta, t2: timedelta)`、`build_step_handlers(...)`、`register_human_handoff(...)`、当前 API `Phase1RuntimeSettings`、`SchedulerWorkerConfig`、`PilotPolicy`，保留旧调用兼容。
- Produces: 可选显式负责人周期设置及完整运行能力，操作文档记准确字段；初始创建时选择模式，不默改旧运行的行为。实现报告给出全部新增公共签名、策略不可变/兼容条件及覆盖文件。

- [ ] Step 1: 先读就近 AGENTS、HANDBOOK 与 spec，确认通知投递如何与已接受事实串行，记录方案；若必须扩大边界，先报告控制者。
- [ ] Step 2: 写真实行为测试，先见 RED。验证新模式不通知经理/老板、2h 边界、接受提交后抑制延迟 Outbox 与排队通知、错误租户/旧负责人拒绝。测试的业务时间由用户确认值提供，其他既有策略仅用隔离合成测试配置。

```python
interval = timedelta(seconds=7200)
# 控制时钟，不实际 sleep 两小时；结果以真实站内任务/存储为准。
# 关键预期序列：初始通知；+7199s 无重复；+7200s 一轮；+14400s 第二轮。
# 接受后继续扫描/投递，实际站内提醒数不能再增长。
```

- [ ] Step 3: 实现显式模式与配置贯通，复用原幂等/持久定时器；为当前事实竞争增加最窄一致性接口。保持旧 v1 与既有入口兼容。
- [ ] Step 4: 运行新增完整链路和受影响旧模式测试，并按失败修复。不要重复无关全库。

```sh
.venv/bin/python3.12 -m pytest tests/unit/test_human_handoff_flow.py tests/integration/test_owner_handoff_reminders.py -q --tb=short
.venv/bin/python3.12 -m ruff check <本任务变更的Python文件>
.venv/bin/python3.12 -m mypy <本任务适用的变更模块>
.venv/bin/python3.12 scripts/check_boundaries.py
```

- [ ] Step 5: 更新已实现/未实现的准确操作文档，记录实际测试命令、版本、结果与残余限制，检查 diff 后只提交本任务文件。

```sh
git diff --check
git add -- <本任务精确文件列表>
git commit -m "feat: remind handoff owner until acceptance"
```
