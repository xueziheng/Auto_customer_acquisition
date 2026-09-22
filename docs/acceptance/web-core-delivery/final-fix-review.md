- **R1：受托回复原件/模型/分类缺当前授权及 Gateway** — **ADDRESSED**。`apps/scheduler_worker/adapters/reply_current_access.py:34` 每次按配置绑定的 tenant/employee 经受信 EmployeeService 查询当前事实，57 行复用原 active boss `qualify` 检查；缺失、停用、降权和跨租户拒绝，system lookup 本身不授予资格。60 行 `require` 在 Message 查询前后重新核资格，并核精确入站/出站关联。
- **R1：原件必须在真实 Gateway 成功后交付** — **ADDRESSED**。`apps/scheduler_worker/adapters/reply_gateway_content.py:65` 注册原 `inbox.message.evidence.read` handler，66 行构造本 runtime 的原 Gateway、权限检查和真实 PG UoW；83 行 load 先取得当前 actor，再走原 `ToolGatewayInboxEvidenceReader`。复用 Inbox 与 qualify 的权限交集，不扩角色、不伪造 review_id；原 EXECUTING 前审计、Message/tenant/actor/task 绑定、4 MiB 有界读取、完整性检查、读后授权、SUCCEEDED 同调用领取和 finally 清槽仍成立。
- **R1：模型前后及分类持久化窗口** — **ADDRESSED**。`workflows/reply_qualification/steps.py:147` 在 Raw 前核 `subject_ref == message_id` 和当前资格，156 行模型前再核，192 行模型返回后取得当前 actor，208 行明确传入原分类服务。`domains/conversations/service_impl.py:327` 在分类原事务内先 `_message_access(...lock=True)`，341 行重核 active boss qualify，再核真实入站/出站，364 行取得原 message 幂等锁；授权在旧分类 no-op 之前。核对原 `infra/db/inbox_access.py:70`：ownership 后按 ID 排序取得当前 actor/owner 的 `FOR SHARE`；同 session 的 Conversations UoW 提交/回滚后才关闭。因此先提交撤权会拒绝分类，已获锁的分类先完成，撤权等待后生效；不是仅靠 wrapper 的时点检查。
- **R1：旧接口兼容不会成为 canonical fallback** — **ADDRESSED**。`apps/scheduler_worker/reply_composition.py:65` 无条件构造 CurrentReplyAccess，68 行无条件构造 Gateway reader，并将 access 注入 ReplyQualificationComposition；`apps/scheduler_worker/runtime.py:1569` 继续传给原 handler。旧显式 actor-less/classification_access=None 低层端口的兼容分支不被该 factory 选择。共用 MIME 投影函数保留原完整候选 guard、长度预算、当前表达隔离及逐字证据；后续动作自身授权仍在 `reply_composition.py:150`。
- **M1：受控回复模型 SQLite 连接未确定关闭** — **ADDRESSED**。`infra/controlled/reply_model.py:22`、48、63、76 四处均为 `closing(self._connect())` 加原事务上下文：先完成 commit/rollback，再 close；未配置响应的调用记录及固定错误语义保留。`tests/unit/test_controlled_reply_model.py:50` 的四方法 × 正常/异常共 8 个参数场景断言 close 恰好一次及原事务分支，原耐久/未配置调用计数测试仍保留。

### New Breakage in the Fix Diff

- **None。** 完整限定 diff 未发现新增 Critical、Important 或 Minor。新增 ADR0066 与就近 AGENTS 收紧当前授权，没有放宽根硬边界；领域没有跨域导入员工实现，仍消费原事务安全事实端口。分类锁序复用原 Inbox 访问顺序，未引入与现有纠正入口相反的 message/员工锁序。

### Out-of-Scope Observations

- **None。** 未重开全分支审查。M2（64/65、10000/10001 精确目录阈值直接测试）和 M3（disabled Worker 实际 adapter 内部 await 取消直接测试）继续按原最终报告 parked；其成本、未来规则变化/生产启用前补测条件不变，不阻断本机受控 Web 交付。

### Checks and Evidence

- **范围检查完成：** 使用 `subagent-driven-development/re-review-prompt.md`；读取 final-fix-brief、final-fix-report 与完整 `review-21d3a3b..ab5eee4.diff` 共 1526 行。FIX_BASE=`21d3a3bb31f2419957668cb65f9eedce613686be`；源码=`02506e44b52392879ea7b8a5b350da435a9f827f`；报告 HEAD=`ab5eee46eaee6f0c4395cb08a64d4874658a8978`。未重读原全分支 diff；仅因本次新增分类持锁/预算的具名风险，定向核原 `_message_access`、Inbox facts 锁、Conversations UoW 提交及 4 MiB 常量。
- **R1 覆盖核对完成：** `tests/integration/test_reply_completion.py:1123` 新参数用例实际经过 canonical factory、真实入站/工作流/PG/Gateway。排队后 inactive/sales/missing/foreign_employee 为零 Raw/模型/新分类；after_raw 阻止模型；during_model 和 persist_wait 阻止新分类；subject/outbound 不匹配及异租户拒绝；真实 PG trigger 注入 EXECUTING 审计失败阻止 Raw，SUCCEEDED 审计失败阻止交付模型；authorized 对照断言新分类及 succeeded ledger。persist_wait 通过第三连接观察实际 PG blocking，持员工排他锁后提交停用，检查等待 share lock 的分类被拒。
- **测试记录核对完成：** 实施报告列出对应 RED/初次 GREEN/边界修正及最终完整受影响 13 文件命令，最终 **216 passed，73.69 s，0 failed/0 skipped，汇总未报告 warnings**；原完整受控主链 **1 passed，35.93 s，0 failed/0 skipped，汇总未报告 warnings**。报告区分 Node 环境 setup errors、跨租户测试初稿的非法复合 FK 设置与产品 RED，没有据此放宽生产隔离。未将局部次数相加为全量。
- **主链 runner 核对完成：** 已读本波保存的 `output/acceptance/final-fix-20260907/e2e_runner.py`，仅在收集阶段替换原测试模块 EVIDENCE 目录，设置禁 dotenv/required E2E，再运行原完整文件，不改业务断言。主链的独立审批、联系人验证、未知发送/重放、接管及清理仍由原测试执行；新结果不宣称新的 UI 视觉覆盖或真实 Provider 验收。
- **源码完整性核验通过：** 实际计算 `tested-source-hashes.json` 列出的 13 个本波 Python 源码/测试文件，全部匹配，mismatches=0。已读 `source-commit-verification.json`，其记录的对应源码 SHA 为 `02506e44...`。未重新执行 Git 命令，也未将此 hash 检查冒称测试运行。
- **静态证据核对完成：** 已读 `static-gates.json` 的精确命令与输出：Ruff exit0；mypy 62 files/exit0；结构七组检查 exit0；staged sensitive scan exit0；cached diff-check exit0。后者 Git stderr30 行作为已记录 AppleDouble 噪声，不计业务 warnings。没有维修共享 `.git`。
- **清理证据边界已保留：** 已读本波 `cleanup-verification.json`：主链 owner stopped、无 cleanup errors、无同出生时间实例存活、两个精确容器 absent；SQLite 文件与侧文件由原 E2E finally 断言。完整回归四个基础设施 owner 的精确清理、主链 proof 由实施报告记录，controller 已核；其基础设施 status 仍为 starting 不被改写成 stopped。此复审没有重新探测进程/Docker，也不声称审计所有历史资源。
- **本次未运行测试/资源：** 没有重跑相同版本测试、启动服务、数据库、浏览器或 Provider；未读取 .env、真实凭证、DSN、Cookie、SQLite 内容、dump 或原始敏感日志；未改源码/index/HEAD、提交、派子代理。只新增本限定复审报告，原初审原文保持不变。

### Verdict

- **Fix round：All findings addressed, no new Critical/Important breakage。R1、M1 均关闭；本波无残余待修项，无第二 fix 波请求。**
- **Spec：Approved。Quality：Approved。** 结合原完整全分支审查，本机受控 Web 在既定限制与 M2/M3 parked 记录下可交付；当前代码无已确认的合并阻断项。这是审查结论，不表示已执行 merge/push/部署。
- **证据版本限制保持：** 新源码 `02506e44...` 仅有本波完整受影响回归和原完整受控主链；后端全量 9318、Web 411 仍归历史 `48e4465`，不能称本修复 HEAD 全仓同轮绿色。没有新增组合疑点要求再跑无改动 full suite。
- **授权语义限制保持：** 已经获准开始的外部模型输入无法事后撤回；此修复提供模型前当前检查、模型后拒绝新分类与 PG 事务内撤权顺序，不承诺分布式 fencing。
- **交付范围限制保持：** 真实 Provider/真实客户发送/供应商联系、共享认证与部署、桌面、通用 Agent/Browser 生产消费者、Catalog queued 后消费者仍 disabled/not_run；Mac 与独立 Linux 报价环境、静态 owned 备份、unknown 成本/token/人工工时口径均沿原最终报告，不因本轮通过扩大。
