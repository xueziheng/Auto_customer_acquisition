# 负责人提醒子项目最终全分支审查

## 审查范围与结论

**最终 spec/plan 功能与结构合规：✅ 通过。可交付本子项目代码：是。Ready to merge（本范围技术审查）：Yes。**

审查基线 `e6b2446909f435ac6dd0250d992555f2c764a741` → `b52f833244560e7e22d18b562519a2120d2f277a`，包含设计/计划、实现、死锁修复三个提交，共28文件。Critical 0 / Important 0 / Minor 0；本子项目没有 deferred、parked 或未关闭的审查项。此结论不表示已推送、合并、部署或初始化真实 profile。

本轮独立核对完整 package、final-review-context、progress 全部 Ruling、最终 spec/plan 和操作文档，再与已审代码行为对应。完整 package 的23个非 docs 文件终态 blob 标识均匹配初轮源码审查与 fix round 1 已审版本；没有未知源码文件或端点差异。因此复用此前源码检查，不重复同一差异与测试，并非把 Task Approved 自动转写成最终 Approved。

## Strengths

- **授权与范围表述一致。** `docs/superpowers/specs/2026-09-08-owner-handoff-reminders-design.md:5` 明确7200秒、接受停止、员工持续负责；负责员工受众是已说明的保守假设，UTC经过时间不冒充工作日历。`docs/operations/web-pilot-policy-decisions-2026-09-08.md:17` 记录用户后续明确的停止条件，`:54` 的交付更新未把主动退回、Agent接续或金额分档关闭写成完成。
- **配置链路是真实能力。** `infra/pilot/config.py:293` 持久配置映射、`apps/api/pilot.py:59` 的实际pilot适配、API/scheduler parser和装配使用同一显式周期。`workflows/human_handoff/flow.py:81` 建立独立负责人模式，未使用超大T1/T2或隐藏7200默认；旧必填商业政策仍严格要求。`tests/unit/test_pilot_profile.py:310` 与 `tests/integration/test_owner_handoff_reminders.py:447` 分别提供配置重读和真实运行根证据。
- **接受后的停止条件落在实际提交边界。** Workflow 每轮经领域当前事实 scope（`workflows/human_handoff/flow.py:221`），最后站内 append 再经相同事实边界（`infra/db/repositories/in_app_notifications.py:35`），独立站内 commit 在释放事实锁前完成。只依赖接受事件消费或只停止扫描的竞态没有留在新模式中；历史通知、owner和handoff事实均不被提醒流程改写。
- **独立审查发现的问题已有真实修复。** Employee 锁改为兼容 FK KEY SHARE 的 NO KEY UPDATE（`infra/db/repositories/opportunities.py:585`），真实转移历史竞争 `tests/integration/test_owner_handoff_reminders.py:589` 断言转移提交且旧受众被抑制；`:703` 确认员工停用仍与在途站内提交串行。ADR0069 `:33` 撤销错误的初版锁判断并准确记录成本，不用“去掉锁”掩盖竞争。
- **兼容策略与运行时一致。** `docs/adr/0069-owner-handoff-reminders.md:10` 永久约束v1及周期编码版本空间；API lifespan和scheduler真实启动拒绝不兼容active Run，不修改通用engine。无active、三类不兼容active、同版本active正向的入口证据分别在 `tests/integration/test_owner_handoff_reminders.py:447`、`:503`、`:677`，先前正向证据缺口已关闭。
- **跨层职责收敛。** 当前接管/归属/active规则留在机会领域服务；infra提供tenant-bound事实锁和站内写入；apps装配真实job与guard；非进程composition_support共享机械notifier，保留scheduler导出且不共享运行对象。新模式移除旧初始投影（`apps/scheduler_worker/runtime.py:808`），新原因生产路由仅选站内（`apps/notification_worker/runtime.py:154`），不会通过另一渠道绕过新停止条件。

## Issues

### Critical

- 无。

### Important

- 无未关闭项。初轮 I1 已由 b52f833 修复，复审结论见 `task-1-fix-1-review.md`；同版本active实际启动证据提示也已 ADDRESSED。

### Minor

- 无新增项。计划中的步骤勾选作为原实施计划记录，完成状态由 progress 的 Task1 complete 与实现/复审报告提供，未据此误判功能尚未实施。

## Plan/Ruling 对照

- 聚焦提醒、暂不实现其他内测政策：spec `:7`、操作文档 `docs/operations/web-internal-pilot.md:188` 与最终代码范围一致；没有将范围外已确认需求删除或宣称完成。
- 显式新模式与旧模式兼容、非工作日历：flow、配置和操作文档 `docs/operations/web-internal-pilot.md:174` 对应；7200不是缺失SLA、积压阈值、金额档或证据映射的默认值。
- 允许新增机械notifier、pilot真实映射和API异步启动检查：实施计划文件清单及现有diff覆盖控制者批准的精确扩展；未扩成进程互导、通用engine重构或新增外部Provider。
- 持久版本编码与拒绝不同active策略：最终spec `:25`、ADR0069 `:10`、运行检查及真实入口测试一致。未来结构必须新workflow type、停止全部应用后恢复旧配置完成旧Run再切换，这些维护成本没有隐藏。
- 当前账户归属必须纳入事实：领域同时比较接管指派、机会owner、账户owner和active；事实缺失报错，不一致/已接受抑制旧受众，不自动修复归属。失败不表示已投递；站内行才是送达证据。最终spec `:29` 与ADR0069 `:8` 准确区分缺失和不一致。
- 多连接且无反向等待：只认定已核对的接受、真实transfer历史FK、员工停用交互；修复不声称未来所有事务无死锁。无外部网络处于该scope内，符合本次窄持锁边界。

## 测试与过程证据

- 复用初轮最终报告：受影响两组单测274和107通过；PostgreSQL20通过（新增14+legacy6），两条未改schema迁移测试明确deselected、无skip；ruff21文件、mypy17模块、boundaries及diff-check通过。
- 复用修复HEAD报告：完整提醒PG文件17通过（原14+新增3；6.33s），flow21通过，ruff2文件、mypy1模块、boundaries及diff-check通过。上述覆盖组存在重叠，不累加成新的“全库测试总数”；未声称本轮重复执行所有初轮套件。
- 初轮审查者唯一owned PG probe在旧HEAD复现40P01，随后实现者回归测试在修复前RED、修复后GREEN，限定复审已核对断言。该缺陷复现记录不冒充通过证据。本次最终gate没有新增测试、实验、资源操作或原始材料读取。
- 实现报告已明示初期两次材料处理偏差（无凭证FileNotFound原始栈、既有测试示例连接串显示）；后续捕获与脱敏纠正，本轮没有新增暴露。不能据最终功能合规声称整个执行历史从未偏离材料约束；本报告不复制原始材料，也不把历史输出重新带入整改范围。

## Recommendations 与交付限制

- **可交付的是提醒子项目代码与合成持久链路验收。** `docs/operations/web-internal-pilot.md:174` 的操作说明足以说明如何在完整政策中显式启用新字段；本次未初始化真实profile，也未证明真实人员日常运营、客户需求验证或北极星指标改善。
- 模式/周期不能热切换；API与scheduler必须来自相同构建和配置，启动检查不替代运维保持配置一致。遇冲突须按ADR恢复原配置完成旧运行，不直接改Run/version/幂等键。没有schema变更或自动迁移工具。
- 连续UTC两小时可能跨越非工作时间；长期停机沿用绝对轮次补进，没有新增合并补发/限频策略；短事实锁与独立站内事务会使接受/停用等待在途提交。这些是明确记录的本次选择及成本，不作为未完成的必修项。
- 主动退回、Agent真实接续、金额分档关闭及首次真实profile所需其他政策仍待后续独立工作。它们是本子项目明确范围外需求，不是被parked的当前审查缺陷；对用户交付说明必须继续保留这个区分。
- 本轮没有运行全仓、浏览器、真实Provider或真实运营验收；没有推送、合并、改源码/index/HEAD、读取凭证或派子代理。

## Assessment

**Ready to merge：Yes（此三个提交构成的负责人提醒子项目，技术审查通过）。**

最终设计、实现、并发修复、兼容策略和操作披露一致，既有唯一阻断已得到真实行为修复与复审，没有新增必修项。实际业务启用仍需完整显式政策和独立运行验收，不能把本结论扩展成“全部内测需求完成”。
