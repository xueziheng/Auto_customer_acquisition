这是对4f83fa2的历史全分支审查。报告中的阻断项已在ad8483a修复并通过最终定向复审；当前结论见[最终复审](agent-upgrade-fix-review.md)。一处合成凭证赋值示例仅改写为文字描述以符合静态扫描，其余审查结论不变。

---

# TradeOS 升级最终全分支审查

日期：2026-10-22。BASE `22ece5a86d374795437407a67154ceba2973e1b9`，HEAD `4f83fa2265b2642361e57cb78880b8e3330d979e`。本报告是全部任务门禁后的唯一全分支审查，不是 Task6 局部复审。

**Ready to merge? No。Critical 0 / Important 2 / Minor 2。** 两个 Important 均有窄复现，应进入控制者的一次集中修复波次；过去任务通过不覆盖这两个跨路径组合缺口。

## 审查方法与证据边界

对照实际规格、计划、最终控制简报、progress 的22项裁定和 deferred/parked 记录，按模型身份与输入、私有搜索与公共交付、来源与服务计划恢复、自动准备与邮件持续对话、兼容与保存证据分层检查。读取各任务报告/初审与修复复审中的最终结论和证据，沿高风险控制流检查当前源码；未把历史未完成检查点当作最终状态。

全分支差异为884文件、106622新增/9763删除，含大规模冻结旧源码导入。审查范围没有排除这些导入文件，但本次是风险与证据驱动审查，**没有逐行阅读全部10万余行导入及测试/生成代码，也不宣称形式化证明全部功能正确**。全量原包保留于 `review-22ece5a..4f83fa2.diff`。对未直接检查的外围能力，结论限于既有报告和保存回归证据，不以来源一致性替代行为验证。

没有重新运行已通过套件，没有子代理、真实模型/来源/邮箱、付费、配置、生产迁移或部署动作。只执行两个无网络窄复现，使用实际被审方法和合成端口，`PYTHONDONTWRITEBYTECODE=1`，没有修改源码、index或HEAD。结束读取HEAD仍为4f83fa2；原未跟踪 `.playwright-cli/`、`output/` 保留。唯一写入为本审查报告。

## Strengths

- 企业确认资料与对外产品权限在实现中分离。`workflows/enterprise_knowledge/confirmed_context.py:35` 重读当前企业、员工、确认版本并保留facts/inferences/provenance；`infra/db/prospect_inventory.py:1276` 交付只接受本任务完成适配、相同query和证据版本的候选。不会凭公共库存数量宣布私有目标满足。
- `infra/model_catalog.py:85` 的bound_view只读当前归档绑定，不建run pin或解析密钥；`infra/model_settings_summary.py:23` 使用它展示真实平台连接。`infra/tenant_model.py:8` 将受信身份与固定run版本接到实际连接，当前业务权限另行检查。企业旧模型写入口的收口与保存测试已有独立证据。
- `apps/scheduler_worker/standalone_inventory.py:131`、`:152` 已将高级档30与平台24/更低policy取min；`apps/scheduler_worker/scheduled_inventory.py:41` 识别当前服务期原请求预留并重放原数量。Task6 I1/I2 的修复仍在，不能与本报告的另一处来源级事务窗口混为一项。
- 搜索后端与页面端口已分开。显式DeepSeek政策不会静默回退Tavily；旧无policy配置的Tavily路径有兼容依据。`infra/db/inventory_search_readiness.py:52` 区分当前连接验证与当前精确政策下的执行历史，GET不产生供应商调用。源码支持DeepSeek不等于现有生产配置已经切换。
- 普通回复采用独立授权、执行租约、不可变计划和HIGH Gateway插件。`tool_gateway/handlers/dialogue_send.py:134` 在执行/对账前重读绑定，unknown走reconcile；`connectors/qq_mail/reply_client.py:86` 对账未命中不会发送。对账绑定线程、双方和内容摘要，未宣称重新下载Sent正文独立证明真实性。
- `apps/scheduler_worker/dialogue_admission.py:103` 在准入内部重读SENT、真实分类、已完成workflow、双方/原件及可继续工作；COMPLETED不复活，REPLIED与其他停止终态区别保留。`workflows/reply_qualification/dialogue_authority.py:81` 在准入前及每轮检查verified联系人、公开产品hash和抑制。
- Task5 I1产品模型输入检查、I2已归档未桥接新邮件fence、I3真正首次inquiry准入均有实际修复与后续整合证据。`infra/db/dialogue_owner_access.py:84` 在原件读取/模型/发送复核中识别pending入站；`apps/scheduler_worker/dialogue_driver.py:207` 废弃尚未派发的旧执行。原O1通过`dialogue_business_inbound.py:97`精确sender/recipient/owner约束关闭，错双方header不再暂停合法binding。

## Issues

### Critical

无已确认发现。

### Important

**I1 [P2] 候选预算提交与来源started检查点之间中断，恢复会终止全部来源。**

位置：`apps/scheduler_worker/inventory_acquisition.py:388`（下一行开始调用reserve_source）；`infra/db/inventory_service_plan.py:103`；前者外层PermissionDenied分支将任务标记collector_failed。

服务计划下，driver先在独立事务中提交source候选预算，再调用backend.reserve_source持久化来源started。若两者之间取消/进程退出，持久预算已经存在，但source_progress没有该来源。新租约重启仍选择该未started来源，并再次reserve_execution；同digest被当成未知外部执行拒绝。异常不是跳过当前来源，而是直接失败整项任务，后续明确尚未调用的来源和剩余额度也不再执行。这破坏Task3的剩余来源恢复语义；只有引入Task6服务计划预算后才出现组合窗口。没有重复外发风险，但用户承担已扣额度及整项目标失败。

窄复现使用实际InventoryAcquisitionDriver、现有RecoveryBackend/Collector和模拟独立持久预算（重复digest拒绝规则与真实reserve_execution一致），在reserve_source入口注入CancelledError，然后新lease重建driver。结果：

```text
after crash: {'industry_directories': 5} source checkpoints: {} external calls: []
after restart: ['claim', 'claim', 'collector_failed'] source checkpoints: {} external calls: []
```

此复现证明实际driver控制流，不冒充真实PG崩溃测试；真实事务与重复拒绝依据上述生产源码。修复应使候选预留与来源派发检查点可原子组合，或在可信未派发证据下识别原预留，以原身份/原数量幂等接续；对已经started且结果未知的来源仍不得重派。至少补真实持久预算提交后、source检查点前失败的重启用例，并断言不二次扣额、其他未调用来源继续，以及真正unknown仍不调用第二次。不能简单删除重复键保护、清空账本或换来源/请求身份规避。

**I2 [P1] 搜索自然语言目标可携带凭证标记直接进入模型。**

位置：`domains/prospecting/inventory.py:65`；`apps/scheduler_worker/inventory_model.py:388`–`:392`。

新intent字段只限制长度；InventoryDiscoveryModel把完整query序列化进实际ModelRequest。搜索/适配入口没有CredentialMarkerGuard，GatewayModelGenerator只校验身份、大小、JSON与额度，Codex请求编码也不检查凭证文本。员工把含password/token/API key的文字误贴进目标时，即使它不进入公共搜索词，也会先进入模型。私有上下文不公开给搜索供应商与模型永不接触凭证是不同要求。此前Task5对公开产品输入的修复没有覆盖该搜索入口。

窄复现使用合成字符串 `含密码字段的合成测试占位字符串`，经真实InventoryQuery.model_validate成功；调用实际InventoryDiscoveryModel.generate，仅将权限/配置与生成器换成无IO端口捕获请求。原CredentialMarkerGuard拒绝同一字符串，但生成器捕获到完整query.intent：

```text
existing guard rejects synthetic marker: True actual InventoryDiscoveryModel forwarded intent to generator: True
```

没有读取真实凭证、创建配置目录或调用模型。沿实际下游检查了`tool_gateway/handlers/model_generate.py:279`及`connectors/codex/client.py:92`：不存在替调用方兜底的凭证检查。问题不限于UI，历史持久请求和API输入也能到该边界。

修复应在构造/派发搜索与适配ModelRequest前，对实际完整用户可控文本投影执行现有确定性凭证检测，覆盖intent、公开标签/候选证据和确认资料实际入模字段；可在API入口额外早拒绝，但不能只检查UI或假设数据以前已净化。不应打印命中内容或原始异常。补正常自然语言目标通过、常见标记/key形状拒绝、持久旧任务恢复拒绝和生成器零调用断言；保留私有目标不流入公共搜索的现有保护。

### Minor

**M1 [P3] 最终验证告警应分流，不能称零告警。**

- `infra/db/repositories/compliance.py:413`：旧`.distinct(column)`生成DISTINCT ON已被当前SQLAlchemy标为deprecated；保存PG日志5条告警，查询仍运行。属于依赖升级兼容债，应在独立兼容维护中改用受支持表达式并保留按国家取最新版本测试，不因本次告警宣称租户泄露或当前查询错误。
- `/tmp/tau-task6-final-linux4.log:5`：离线测试依赖包缺chardet/charset_normalizer，RequestsDependencyWarning；这是测试运行包完整性问题，83个离线协议/CLI测试仍通过。补离线依赖包后在下一次相关验证确认消除，不推导生产HTTP内容解码已验收。
- `docs/operations/agent-upgrade-verification.md`披露Web PlatformConsole静态/动态导入分块提示与1191条风格告警。属于分包/维护债，非已确认本次业务回归。保留0 errors/1191 warnings原数，不批量改格式掩盖本轮修复。

本项合计一个Minor，与旧任务M1对应。Task2 Alembic path_separator及UI替身噪声已进入后续修正/最终验证记录，不另造开放阻断项。

**M2 [P3] 根总纲仍要求“已发布产品”作为搜索起点，与已接受ADR和本次实现不一致。**

位置：`AGENTS.md:10`–`:12`，以及同文件当前阶段的“产品完善并发布→探索”；对照`docs/adr/0113-product-first-agent-upgrade.md`决策、`docs/superpowers/specs/2026-10-21-tradeos-agent-upgrade-assessment.md:5`的“已同步总纲”声明。

根总纲是后续Agent最高规则入口，却继续使用2026-10-07/ADR0081的已发布Product门槛；实际已确认资料私有探索明确不要求对外发布。后续实施者可能据此给搜索错误加发布前置条件，或把内部确认误当发布。应按已接受ADR0113统一根业务入口、资料确认/公开产品的区分和当前验收状态；九条硬边界不改。该问题不代表本次实际准入已经错误，按文档一致性Minor计。

## 既有证据与deferred结清

- 读取`controller-preservation-checkpoint.json`与baseline provenance：导入时704路径中701相同、3仅空白；当前670保留、34废弃构建备份有明确7715b40清理记录。冻结验收副本2947文件0改变/0缺失，54来源registry同hash。以上来自控制者保存哈希核查，本审没有重新遍历原运行配置或读取密钥，也不把保存正确当逻辑正确。
- 读取真实保存日志：首次Task6整合HEAD的PG302通过/5告警、Linux83通过/1告警；最终4f83fa2相关修复批次201通过20.52秒。没有把这些数相加；10002 unit/evals、Web691与parser29以及mypy955/ruff/结构/scans采用最终报告和既有控制者核验，未重复执行。
- 读取Task7保存live JSON：仅TED供应商HTTP1次、1个notice；Tavily usage_unknown未运行、DeepSeek official_current_version_missing未运行，page/model/business/email均未执行。技术诊断账本不等于真实业务写入，样本不证明合格客户或获客效率。
- Task6 I1高级档构造/I2周期原请求回执恢复已关闭；O1精确双方spoof暂停已关闭；M1在上文分类处理。22项裁定没有发现被本次建议扩大权限；本报告I1/I2均执行既有恢复/凭证边界，不需新业务授权。

## Declined to judge

以下逐项交控制者裁定，不能默认为已验收：

1. 真实DeepSeek/Tavily当前API可用性、当前收费/免费资格及真实模型质量：本审明确禁止重新live调用；只能确认已有源码/测试与有日期的保存证据，不能从合成响应推导线上可用。
2. 真实联系人验证、邮箱送达率、发件信誉、真实客户多轮沟通和单位合格机会成本：无真实运营实验，邮件协议替身与合成Need证据不能回答这些问题。
3. 真实部署配置、当前生产API key引用是否可解析、实际运行库迁移/回退和生产多企业隔离：没有部署/配置访问授权；本地两个企业真实角色PG测试不等于生产验收。
4. 各国家商业触达合法性及处理依据真实性：代码验证引用、政策与审批存在，不对真实法律事实作结论；没有进行外部法律核查。
5. 未触及的全部历史报价、供应、私人测试与外围UI导入逻辑的逐行正确性：全分支风险审查涵盖相关连接边界并采纳保存回归，未逐行穷尽所有导入代码；不据此新增无证据缺陷，也不将其宣布全部已人工审过。
6. 邮箱持有者恶意修改Sent正文但保留摘要头的真实性证明：连接器契约只核对本机写入的精确头绑定，规格未要求抵抗已获完整邮箱控制权者；当前证据不能证明此类篡改不存在。

补充已调查但未形成缺陷：旧run在平台切换连接后沿归档版本继续是明确兼容契约；模型当前业务撤权由独立authority承担，不能把单纯切换连接解释成所有旧run自动撤销。当前资料/owner改变后对账可能保留人工核对，未把“为了对账继续读旧员工邮箱”当作合理默认授权。两项均不建议放宽既有权限。

## Recommendations / Assessment

一次集中修复I1/I2，给M2做窄文档同步；M1可作为明确维护债保留。修复后只重跑受影响恢复、模型输入/私有投影及必要集成检查，再做一次针对最终修复差异的复审，不要求重复所有既有绿套件或触发live。

**Ready to merge? No。** 源级恢复窗口会使合规剩余来源不可恢复，搜索intent模型入口遗漏硬边界1；两项修复并经聚焦复核前，不应合并。此判断是源码交付门禁，不是生产发布许可。
