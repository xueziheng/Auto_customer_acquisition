这是ad8483a提交时的历史修复报告，随后已通过[最终复审](agent-upgrade-fix-review.md)。报告提到的24项裁定与六项证据限制完整保存在[验收记录](agent-upgrade-verification.md)。

---

# 最终集中修复交付报告

状态：READY_FOR_SCOPED_REVIEW；已本地提交，等待一次定向独立复审，不自行声明评审通过。

工作区 `/Users/xueziheng/.codex/worktrees/tradeos-agent-upgrade/Auto_customer_acquisition`，分支 `codex/tradeos-agent-upgrade`；原工作区和冻结验收副本未写入。本轮独占代码/index/任务PG，未派子代理。

## 最终全分支评审集中修复（2026-10-22）

FIX_BASE `4f83fa2265b2642361e57cb78880b8e3330d979e`；本节仅记录最终修复，不覆盖上述历史全套基线。最终提交SHA及BASE..HEAD由计划区 `final-fix-report.md` 记录。此刻仅READY_FOR_SCOPED_REVIEW，不能视为独立复审通过或生产许可。

- I1：原来源候选预留独立提交保留。恢复先锁当前服务计划和任务，核实原服务期、当前租约、固定来源计划、24h期限以及不存在该来源started/终态尝试；仅复用同digest原预留，原amount不改、不退款、不换身份。返回可执行量由当前上限减全部已用量再加本来源原预留推导，并受driver原来源分配上限约束。已started的unknown仍跳过，无第二次调用。降档原15预留仍记15，实际恢复最多5；后续零额度来源持久标记quota_exhausted而不外调。正常/升档恢复继续剩余未调用来源，累计原10不扩大。
- I2：实际InventoryDiscoveryModel.generate在构造模型组合前，对最终model_payload完整JSON执行既有CredentialMarkerGuard；覆盖query/intent/标签、候选证据和确认资料实际字段。失败仅固定ModelGenerationError(permission)，不回显内容。evaluate经同一边界。真实旧请求持久化、过期领取新lease后仍拒绝；实际生成器零调用。正常输入及原公共搜索隔离验证保持。
- M2：根总纲入口与当前阶段按已接受ADR0113同步，确认资料可私有搜索，确认不等于正式Product发布、Campaign批准或对外承诺。九条硬边界及商业审批未改。
- M1：按裁定23转维护；上文原SQLAlchemy 5告警、离线依赖1告警、Web分块提示与1191风格告警仍存在，未批量清理。本轮未重跑Web/Linux/10002全套或真实来源诊断，相关数量不与本轮相加。

本轮最终命令：

```bash
.venv/bin/python -m pytest tests/unit/test_inventory_model.py tests/unit/test_inventory_source_recovery.py tests/unit/test_inventory_acquisition_driver.py tests/unit/test_inventory_enterprise_context.py tests/unit/test_inventory_search_policy.py tests/unit/test_inventory_query_binding.py tests/unit/test_inventory_search_guidance.py tests/unit/test_inventory_standalone_composition.py tests/evals/test_inventory_evidence.py -q
.venv/bin/python /tmp/tradeos-agent-upgrade-20261021/fresh_test_database.py tests/integration/test_inventory_source_recovery.py tests/integration/test_inventory_service_plan.py tests/integration/test_inventory_model.py tests/integration/test_inventory_search_budget.py tests/integration/test_inventory_search_readiness.py --basetemp=/tmp/tau-ff-pg4 -q
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy domains shared tool_gateway connectors apps workflows notification_gateway infra
.venv/bin/python scripts/check_boundaries.py
.venv/bin/python scripts/scan_sensitive.py
git diff --check
```

|检查|精确结果与退出码|日志|
|---|---|---|
|相关unit/evals|99 passed in 1.74s，exit0|`/tmp/tau-final-fix-unit2.log`|
|真实PG相关5模块|56 passed in 36.61s，exit0|`/tmp/tau-final-fix-pg4.log`|
|全ruff|All checks passed!，exit0|`/tmp/tau-final-fix-final-ruff.log`|
|全mypy|Success: no issues found in 955 source files，exit0|`/tmp/tau-final-fix-final-mypy.log`|
|结构|七类检查通过，结构自检通过，exit0|`/tmp/tau-final-fix-final-boundaries.log`|
|敏感扫描|无命中，exit0|`/tmp/tau-final-fix-final-sensitive.log`|
|差异空白|无输出，exit0|`/tmp/tau-final-fix-final-diff.log`|

RED及中间结果分别保留：

- 将三个实现文件临时还原为FIX_BASE后，新增模型测试命令 `.venv/bin/python -m pytest tests/unit/test_inventory_model.py -k actual_model_dispatch -q`：20 failed, 5 passed, 16 deselected in 0.67s，exit1；`/tmp/tau-final-fix-red-model.log`。正常5条通过，四种凭证×五个输入位置全部未被拦截。
- 同FIX_BASE，fresh_test_database.py仅运行test_inventory_source_recovery.py的`-k service_budget_checkpoint --basetemp=/tmp/tau-ff-red -q`：4 failed, 2 passed, 16 deselected in 1.47s，exit1；`/tmp/tau-final-fix-red-pg.log`。三项未started恢复失败证明原缺陷；降档unknown的额外失败来自过强终态断言，后续按保守未知消耗修正，不算另一缺陷。
- 首次两模块PG：2 failed, 34 passed in 8.33s，exit1，`/tmp/tau-final-fix-pg1.log`。零额度分支不能直接record未started来源，修复其状态顺序；unknown且已耗尽不要求成功终态。
- `/tmp/tau-final-fix-pg2.log`：2 failed, 54 passed in 38.53s，exit1；进程加载旧零额度分支后才完成修改，不能当作修改后失败或通过。一次系统python3编辑脚本因其旧解释器拒绝UTF-8而未执行，已改用.venv解释器执行，未修改仓库解释器配置。
- `/tmp/tau-final-fix-pg3.log`：56 passed in 37.56s，exit0；随后进一步将恢复的当前服务期/政策核对置于计划锁内，最终以pg4为准。
- 初次敏感扫描exit1，2条新合成password赋值命中（`/tmp/tau-final-fix-sensitive.log`）。改为既有识别的placeholder值，保留相同凭证标记负例；未改扫描器、白名单或删负例。最终扫描exit0。首次局部ruff有2个import顺序问题，由ruff --fix修正；最终全ruff通过。

本轮无真实provider、模型、页面、邮箱、配置或生产访问；只用合成输入、协议端口和任务PG。六项证据限制与24项裁定逐条保留于本文件。独立复审仍由控制者执行。

## 审查重点与残余风险

恢复方法不签发外部执行许可：backend.reserve_source仍在锁定当前lease后唯一写started，两个同时恢复者也只能一个成功；未started预留读取不改变预算。原reserve_execution重复键拒绝保持；真实started来源仍无重新调用。预留保守不退款意味着降档可以导致任务提前耗尽，这不是获客成功承诺。凭证guard沿用保守词表，可能误拦普通词；未放宽既有契约。M1按裁定23保留维护债，最终审核六项限制按裁定24保留；无其他已知未处理本轮发现。

根总纲只改业务起点与当前阶段，未改九条边界或人工审批。未更改配置/54来源registry/原UI/Obsidian/历史Task6报告。

## 最终交接

- HEAD：`ad8483ae00961e0c48fb574e51da7d62e46c1296`。
- 精确范围：`4f83fa2265b2642361e57cb78880b8e3330d979e..ad8483ae00961e0c48fb574e51da7d62e46c1296`，单个本地提交，8文件297插入16删除；未push/merge。
- 3130路径显式敏感扫描exit0（仅排除原无关.playwright-cli/output），日志`/tmp/tau-final-fix-sensitive-all.log`；staged扫描exit0、提交前结构扫描通过。根九条边界和人工审批两节与FIX_BASE逐字相同。
- 所有pytest/静态检查进程已退出。任务PG client连接0，日志`/tmp/tau-final-fix-release.log`；index为空、index.lock不存在、tracked工作树干净。原未跟踪.playwright-cli/output保留。
- 即刻释放代码/index/任务PG所有权给控制者；READY_FOR_SCOPED_REVIEW，不自判review pass。
