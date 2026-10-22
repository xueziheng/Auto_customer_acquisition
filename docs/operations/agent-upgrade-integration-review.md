# Task6 集成审查归档

先保留初审，再保留修复复审；I1/I2最终已关闭。M1转维护，未当作已修复。

## 初审（43a1148）

### Spec Compliance

- ❌ Issues found：2项Important影响高级档实际搜索和周期未知提交恢复，见I1/I2。范围：`0df70cc9c8e486598a018b26d573141a62e05a61..43a1148084b0b304e3cd85b1e611bf34716db8bb`。
- ⚠️ Cannot verify from diff：704导入路径（当前670及34生成备份清理）、冻结2947文件、54来源ID、持久配置/邮箱/Obsidian保留属于跨任务事实。控制者应结合`final-review-baseline-provenance.md`、`frozen-import-provenance.json`、`controller-preservation-checkpoint.json`及Task1/2/7门禁核实。本次未读取真实配置/凭证。Task6永久说明见`docs/operations/agent-upgrade-verification.md:9`。
- ⚠️ Cannot verify from diff：TED单次实测、DeepSeek/Tavily未运行原因需对照既有`task7-live-safe.json`及Task7最终报告；原生CLI实际运行证据需对照既有日志，不能由Task6源码单独证明，也无需再次live调用。
- ⚠️ Cannot verify from diff：平台grant、旧run pin、安全`FileModelCatalog.bound_view`及原私有邮箱validator主要在前置任务；本次调用点见`infra/model_settings_summary.py:23`。这些需由全分支评审结合前置任务证据确认，不扩大本任务审查代替全分支门禁。
- ⚠️ Cannot verify from diff：全分支独立审查尚待控制者执行；真实部署与运营效果未验收。`docs/operations/agent-upgrade-verification.md:3`正确保留此边界。

### Strengths

- 企业模型写权限在域服务收口：`domains/assistant/service_impl.py:265`；直接服务和旧POST拒绝测试见`tests/unit/test_enterprise_model_authority.py:29`及`:65`。安全摘要在`infra/model_settings_summary.py:14`选择当前平台绑定，而非以旧SQL模型冒充当前模型。
- 周期复用canonical请求、先恢复收据：`apps/scheduler_worker/scheduled_inventory.py:29`；`infra/db/prospecting_service_plans.py:540`使用独立请求hash，未把acquisition ID伪装成WorkflowRunId。独立迁移`0102`接`0101`并为四表设置RLS，实际租户角色测试见`tests/integration/test_inventory_service_plan.py:46`。
- 邮件复用原email.send与正式QQ连接器。`infra/db/campaign_mail_owner.py:26`绑定真实员工、客户归属、SendingIdentity和邮箱版本；`tool_gateway/handlers/email_send.py:325`在连接器解析前重查owner。`tests/integration/test_customer_preparation_runtime.py:423`覆盖全局off、两步REPLIED和单步COMPLETED，`:429`覆盖两企业完整链。
- `apps/scheduler_worker/dialogue_admission.py:103`在admit自身重读SENT、分类、workflow、MIME/原件和待办；`:155`保持冷Enrollment终态。`workflows/reply_qualification/dialogue_authority.py:81`将当前联系人、产品hash和抑制检查用于准入前。`tests/automatic_mail_scenario.py:260`起的真实scheduler/MIME/同Need断言没有以手工API首封代替自动验收。
- O1通过`apps/scheduler_worker/dialogue_business_inbound.py:97`的双方/owner关联收紧；`tests/integration/test_ordinary_dialogue_factory.py:272`的spoof用例验证错sender伪造header不会暂停合法binding或新增模型/发送。本次未发现该原O1仍被此用例触发。
- `apps/web/src/views/customers/CustomerDetail.vue:254`明确区分公开来源记录和当前验证状态，匹配/不匹配地址断言见`apps/web/tests/customer-workbench.test.ts:696`。永久验收文档`:85`至`:106`包含22项裁定及成本，不把合成验收写成生产启用。

### Issues

#### Critical (Must Fix)

- 无，数量0。

#### Important (Should Fix)

- **I1：高级档搜索预算30被仅接受0..24的构造器拒绝。** `apps/scheduler_worker/standalone_inventory.py:131`及`:152`将`limits["searches"]`原样传给`InventorySearchBudget`。`infra/db/inventory_service_plan.py:72`对高级档新receipt返回30，而`infra/db/inventory_search_budget.py:43`拒绝大于24的值。新高级档DeepSeek/Tavily任务因此不能正常初始化搜索，即使平台政策已正确限制为24。纯构造器复现得到`ValueError: 受信任务上限无效`。现有`tests/integration/test_inventory_service_plan.py:16`覆盖的是basic新任务再升级保留10，未覆盖advanced初始任务。应合并服务档、平台policy、旧持久cap和当前收紧cap取min；保留高级档30的上限语义和平台24限制，不能为修复错误扩大平台额度。补两个后端高级档装配及恢复/降档测试。

- **I2：周期独立扣额后、请求回执提交前失败，会被自己的预留挡住恢复。** `apps/scheduler_worker/scheduled_inventory.py:41`每次重试按已预留总数再加整轮额度；`infra/db/prospect_inventory.py:511`在请求行提交前调用独立事务`reserve_request`。因此预留可以已提交而InventoryRequest不存在。basic此前预留90候选，周期再预留10后请求事务失败，重启时find_request为空、reserved100，41行返回0，永远不到可幂等重放的request，pending保持且不能换目标。窄复现使用真实SQLite Store和真实reserve_request、在回执前注入失败，第二次输出`restart result: 0 request calls: 1 reserved: 100 pending: True`。`tests/integration/test_prospecting_service_plans.py:186`的unknown测试使用不扣额的request替身，遗漏此事务边界。恢复应识别本周期既有当前期receipt并保留原请求身份/数量，不按新增请求再次计算额度；暂停、到期及当前授权仍按原语义处理。补真实请求事务回滚后的恢复测试，并覆盖切档后原请求数量不变。

#### Minor (Nice to Have)

- **M1：最终验证有已披露的遗留告警。** `infra/db/repositories/compliance.py:413`的DISTINCT ON弃用告警在`/tmp/tau-task6-final-pg4.log:13`出现5次；`/tmp/tau-task6-final-linux4.log:5`有字符检测依赖warning；Web build有PlatformConsole分块warning，lint有1191条warnings。`docs/operations/agent-upgrade-verification.md:65`如实披露这些限制。没有将其认定为本任务新增业务回归，也不建议为压制噪声扩大本轮修改；控制者/全分支审查应分流兼容性、测试环境与风格债。数量按一个遗留验证噪声事项计，不将每条lint诊断视为独立缺陷。

### Checks

- 按模型/邮件装配、预算恢复、迁移/UI、测试/文档多轮阅读提供的diff包。没有重新生成git diff，没有修改源码/index/HEAD，没有子代理及真实来源/模型/邮箱/配置/生产访问。仅写本审查文件。
- 两个未覆盖疑点采用一次`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python`窄验证：I1纯构造器，I2真实Store/真实reserve_request与回执前失败注入。SQLite位于自动清理的`/tmp/task6-review-*`，无网络、无任务PG操作，不重跑已保存通过套件。
- 窄验证原输出：`I1 advanced cap: 受信任务上限无效`；`I2 restart result: 0 request calls: 1 reserved: 100 pending: True`。
- 读取既有日志核实PG302通过/5告警、Linux83通过/1告警。控制者另确认最终unit10002、Web691和parser29；这些结果未被重新运行来再生成证据。

### Assessment

**Task quality:** Needs fixes。

**数量：** Critical 0、Important 2、Minor 1。Spec：❌；Quality：Needs fixes。

**Reasoning:** 权限收口、当前事实准入、同邮箱自动首封与真实回复链有具体实现和集成覆盖；服务计划与库存预算组合仍存在可复现的高级档失效及崩溃恢复卡死。修复前不能批准Task6；跨任务Cannot Verify事项交由控制者按既有证据解决，不代表证据不存在。


## 修复复审（4f83fa2）

- **I1：高级档搜索预算30被仅接受0..24的构造器拒绝。— ADDRESSED。** `apps/scheduler_worker/standalone_inventory.py:131`、`:152`分别在DeepSeek与Tavily正式装配构造前取服务档额度与平台policy的min；`shared/schemas/search_backend.py:16`仍将平台上限限制为24，未改变高级档30的服务计划语义。`infra/db/inventory_search_budget.py:85`继续与旧持久cap取min，`:97`的当前cap reader继续只收紧。`tests/integration/test_inventory_service_plan.py:85`起的两后端×平台24/7用例实际调用build_inventory_driver及validate_binding，`:135`至`:147`断言advanced初始化、standard降额、重建后advanced不扩额、basic继续收紧，原预留30不重置。模型身份端口为合成替身，不是供应商live验收。

- **I2：周期独立扣额后、请求回执提交前失败，会被自己的预留挡住恢复。— ADDRESSED。** `apps/scheduler_worker/scheduled_inventory.py:41`先找当前期原预留，只有不存在时才按新增消费前检；`:48`及`:53`重放原候选数量和同cycle key，未改employee/query。当前排程/有效期检查`:37`、当前员工/产品授权`:49`至`:51`保持。`infra/db/inventory_service_plan.py:115`委托租户绑定的有效期查询；`:44`至`:50`允许降档后的同值receipt重放，原仓库`infra/db/prospecting_service_plans.py:335`至`:349`仍在锁下拒绝过期、旧服务期和数量冲突，且同键同值不再扣额。`tests/integration/test_inventory_service_plan.py:188`在真实request已flush后的_allocate处注入故障，`:197`确认InventoryRequest/Job事务回滚且独立预留保留，再重建真实service/store；`:218`至`:228`验证same/upgrade/downgrade同身份同数量、零重复扣额以及当前候选cap，`:229`至`:239`验证pause/expiry/renew/员工撤权/产品撤回不提交新请求。`tests/integration/test_prospecting_service_plans.py:1045`进一步覆盖原数量可重放、改数量/新超额/续期旧键拒绝。当前执行limits仍由`infra/db/inventory_service_plan.py:70`收紧；周期必须有confirmed_refs，因此`domains/prospecting/inventory.py:97`和`infra/db/prospect_inventory.py:475`也继续阻止跳过私有适配直接消费公共库存。

### New Breakage in the Fix Diff

- None。新增Critical 0、Important 0、Minor 0。六文件修复未改变平台付费许可、服务档数值、租户边界或外部调用出口。

### Out-of-Scope Observations

- 无新增。原M1验证告警债继续留待最终全分支审查分流，不计本轮新增问题、不阻塞本次限定复审。原审查跨任务Cannot Verify项仍由控制者按全分支证据核实，本次不重新声明这些事项已验收。

### Checks

- 检查范围：FIX_BASE `43a1148084b0b304e3cd85b1e611bf34716db8bb` → HEAD `4f83fa2265b2642361e57cb78880b8e3330d979e`；读取控制简报、复审模板、原I1/I2、完整fix diff、追加报告及对应装配/预算/周期/请求事务上下文。只核对原发现与fix引入问题，没有进行全分支重审。
- 读取`/tmp/tau-task6-fix1-final.log`：真实保存输出为`201 passed in 20.52s`，与追加报告列明的五个PG/持久仓储模块及五个unit模块命令一致；未重新运行套件。
- 读取保存的完整mypy/ruff/结构日志：`Success: no issues found in 955 source files`、`All checks passed!`、`结构自检通过`；读取敏感扫描日志与`task6-fix1-precommit.json`、`task6-fix1-delivery.json`中的命令/exit0记录。空敏感扫描日志本身不单独当作退出码证明。
- 初始只读status/HEAD核对；未重新生成diff，未运行测试、迁移或联网请求。无子代理、真实模型/来源/邮箱/配置/生产访问；仅写本审查报告，未改变源码、index、HEAD或分支。

### Verdict

- **Fix round:** All findings addressed, no new Critical/Important breakage。
- **Spec:** PASS（本轮I1/I2与fix diff限定范围）。
- **Quality:** PASS（本轮限定范围）；开放Critical 0、Important 0；原M1继续由最终审查分流。
