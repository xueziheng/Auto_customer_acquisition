### Task 1: 负责人提醒完整链路、兼容与验收

**Files:**
- Modify: `workflows/human_handoff/flow.py` 与 `AGENTS.md`：新增明确模式，接受停止与持久重复提醒。
- Modify: `infra/pilot/config.py`、`apps/api/runtime_config.py`、`apps/api/composition/runtime.py`、`apps/scheduler_worker/config.py`、`apps/scheduler_worker/runtime.py`：显式周期贯穿配置与装配。
- Modify: `apps/scheduler_worker/notification_projection.py`、`notification_gateway/templates.py`：新负责人提醒原因与准确文案。
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

## 控制者明确补充

已批准新增 `apps/composition_support/handoff_notifications.py`（先读该目录AGENTS），只共享API/scheduler通知任务机械适配，scheduler兼容导出；不导入其他进程、不共享运行对象。已批准opportunities公共最窄持锁事实接口，在infra实现跨表锁，串行真实站内提交与accept；需实际多连接竞争验证、无域间内部导入、无外部网络持锁及无反向等待。具体rationale/cost见progress.md。

精确追加 `apps/api/pilot.py`：runtime_settings(config)须映射新周期，测试真实pilot入口不丢策略。

## 持久版本裁定

现human_handoff type永久保留v1为旧升级，v2..2147483647编码owner周期秒数+1（输入整数1..2147483646）。所有parser/workflow入口必须一致拒绝非法值；新流程结构未来换type，ADR记载。API与scheduler实际startup在任何活跃同type Run的版本与所选模式/周期不一致时固定拒绝，无静默悬挂/重新解释；指导恢复原配置完成旧Run再改模式。不同模式/周期、有旧v1、正常同版本与无active需实际覆盖。

精确追加 `apps/api/runtime.py`：在真实异步startup调用版本一致性检查；同步composition无IO，失败沿原资源清理并零业务推进。
