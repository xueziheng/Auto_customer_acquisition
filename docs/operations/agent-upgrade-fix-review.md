这是最终一次定向复审的完整记录，范围4f83fa2..ad8483a。M1明确延期，未宣称零告警或生产启用。

---

- **I1 候选预留提交后、来源 started 前中断无法恢复** — **ADDRESSED**。`infra/db/inventory_service_plan.py:131` 新增同身份未派发预留读取：锁当前服务计划及任务，核实 tenant、processing、当前 lease/token/期限、固定来源计划、collection_deadline、无任何来源尝试，以及预留与当前服务期一致。只读取原 candidates digest，不改原 amount，不退款、不换身份；当前档位上限在计划锁内再次取 min。`apps/scheduler_worker/inventory_acquisition.py:383` 复用返回额度，只对没有原预留的来源再次 reserve_execution；原重复 digest 拒绝未删除。
- **I1 恢复后的额度、来源检查点及未知结果约束** — **ADDRESSED**。`apps/scheduler_worker/inventory_acquisition.py:346` 的持久 progress 跳过全部已有来源尝试；`infra/db/prospect_inventory.py:1019` 仍在当前任务锁下检查 prior 并唯一写 started，读取预留本身不授予重复外调许可。`apps/scheduler_worker/inventory_acquisition.py:388` 零额度分支先 reserve_source 再记录 quota_exhausted，符合 `infra/db/prospect_inventory.py:1099` 的 started 前置条件，且不调用 collector；两步之间再次中断会保守留下 started，恢复也不外调。零额度记入时间预留不会侵占超过固定来源计划总额的时间。当前周期失效/续期不能用旧预留恢复，降档只收紧可执行量，旧消耗保留。
- **I1 持久恢复证据** — **ADDRESSED**。`tests/integration/test_inventory_source_recovery.py:512` 的真实 PG 六组合覆盖 reserved/unknown × same/downgrade/upgrade：独立持久预算提交后取消、新 lease 重启、原 amount 和请求总预留不变、每来源最多一次 collector、正常及升档继续剩余来源；降档原15保留、未派发实际最多5、后续零外调。保存最终 PG 日志通过。锁与周期检查由源码直接核对；不把这六个用例宣称为所有并发交错的穷尽证明。
- **I2 完整用户可控模型输入缺少凭证护栏** — **ADDRESSED**。`apps/scheduler_worker/inventory_model.py:364` 先组成实际 model_payload（payload、完整 query、allowed_source、internal_confirmed_context），在构造模型组合及生成器调用前以既有 CredentialMarkerGuard 检查其完整 JSON；命中只抛固定 ModelGenerationError(permission)，不回显内容。`:402` 把同一个已检 payload 传入 ModelRequest，未另加未检文本。evaluate 的实际候选证据也经过 generate；当前 authority 和私有公共投影分离未被移除。
- **I2 搜索、适配及历史任务证据** — **ADDRESSED**。`tests/unit/test_inventory_model.py:131` 覆盖四种合成凭证标记/形状 × intent、标签、payload、确认资料、实际 fit evidence 五个位置，拒绝时生成器零调用；五个正常输入仍通过。`tests/integration/test_inventory_source_recovery.py:591` 从真实持久旧 intent 重新领取 lease，走实际 generate 边界并断言零生成器调用。不是仅靠 UI/API 净化或孤立 guard 测试。
- **M2 根总纲搜索起点与 ADR0113 不一致** — **ADDRESSED**。`AGENTS.md:9`、`:13`、`:170` 已同步企业资料与产品确认、私有搜索及当前验收边界；确认明确不等于 Product 发布、Campaign 批准或对外承诺。对照 ADR0113 与本次 diff，九条硬边界及商业审批两节未改。
- **M1 既有告警维护债** — **NOT ADDRESSED（按控制者 Ruling23 明确延期，非本轮阻断项）**。`docs/operations/agent-upgrade-verification.md:129` 和 `:224` 保留 PG5、Linux1、Web1191风格告警及分块提示，不称零告警。没有把延期伪装成代码修复。

## New Breakage in the Fix Diff

- **None**。新增 **Critical 0 / Important 0 / Minor 0**。本结论仅针对 `4f83fa2265b2642361e57cb78880b8e3330d979e..ad8483ae00961e0c48fb574e51da7d62e46c1296` 的8文件修复差异及必要连接点，不是第二次全分支审查。

## Checks

- 已分段读取完整修复包、原 final-review、final-fix-brief、controller brief、final-fix-report 和适用规则；对照实际生成器入口、预留账本、来源状态写入及计划锁实现。未修改代码/index/HEAD，未派子代理，唯一写入为本报告。
- 已直接读取 `/tmp/tau-final-fix-unit2.log`：**99 passed in 1.74s，EXIT=0**；`/tmp/tau-final-fix-pg4.log`：**56 passed in 36.61s，EXIT=0**。测试命令和五个 PG 模块与修复报告一致，不与历史完整套件数量相加。
- 已读取模型 RED 日志：20 failed / 5 passed / 16 deselected；PG RED：4 failed / 2 passed / 16 deselected。与新增负例和原恢复缺陷对应；报告已单独披露降档 unknown 的旧过强断言及零额度状态顺序中间失败，没有把它们计为最终成功。
- 已读取保存的最终 ruff、mypy955、结构、敏感扫描及 diff 检查日志，均 EXIT=0；显式全路径敏感扫描3130路径 EXIT=0，扫描器未在 diff 中放宽。release 日志记录任务 PG client=0 和交付 HEAD。按复审要求未重跑已通过套件，也未触及真实 provider/model/mail/config/production。
- `docs/operations/agent-upgrade-verification.md:101`、`:132`、`:217` 正确保留24项裁定、六项证据限制和本轮准确结果，状态明确仍待本次定向复审。历史真实来源证据仍仅为一次 TED；未把合成测试或本地 PG 推导为真实运营、法律事实、生产部署或完整邮箱控制权攻击抵抗验收。

## Out-of-Scope Observations

- **None（无新增范围外问题）**。原 M1 维护债及 Ruling24 六项未验证边界如上保留，不静默关闭，也不以未穷尽旧导入逐行审查新增缺陷。

## Verdict

- **Fix round: PASS — All scoped findings addressed, no new Critical/Important breakage。** I1、I2、M2 全部关闭；M1 按 Ruling23 延期。该判断关闭本次修复源码门禁，生产许可与六项证据限制不变，最终交付裁定由控制者登记。
