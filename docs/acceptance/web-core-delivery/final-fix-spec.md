# 最终审查统一修复波次

本文件只用于本次交付，不是平行 Agent 规则入口。完整发现见同目录 final-review.md，控制者已全文阅读。执行 BASE 由实际派发给出。仅修复 R1 和 M1；M2/M3 经最终独立审查认可保留，原因与未来补测触发条件见报告及ledger，不在本波新增其测试。

## 范围与约束

- 本机受控 Web 收口，桌面只保留契约；不执行真实 Provider、客户发送、供应商联系、部署、合并或推送。
- 读取根 AGENTS.md、HANDBOOK.md 及每个进入目录的上级与就近规则。九条硬边界不放宽。
- 使用既有领域公开接口、原工作流与真实 Gateway；新工具只加 manifest/check/handler，不在 Gateway 核心按业务分支。
- 当前 tenant/employee/权限来自服务端持久事实，不能从客户端自报或测试伪造；停用、降权与跨租户必须失败关闭。
- 凭证只在受信运行期内存由已有配置加载器解析；不读取给模型或输出 .env、DSN、Cookie、SQLite 内容、dump、原始敏感日志。
- Decimal、事实与推断分离、Provenance、quoted 报价、逐次审批及 research_only 边界全部保留。
- 所有命令显式 workdir；Python 使用 .venv/bin/python。Git AppleDouble 警告只记 stderr 行数，不修共享 .git。
- 仅使用自己的测试进程和隔离 owner 资源；不批量 kill、prune、清库或清除历史 output。已有 381 个未跟踪 output 是历史证据，不混入提交。
- 不派子代理。本轮是唯一统一修复波；实施者完成聚焦验证与本地提交后，由控制者交一次限定复审。

## 已确认需修复的回复分类授权与读取审计

最终审查初报：CurrentEmployeeReplyFactory 固定配置 tenant_id/employee_id，却将 Raw Store 直接交给 ArtifactMessageContentReader。ClassifyStep 在后续动作的当前员工资格检查之前读取原件并调用模型；停用/降权可能只阻止后续动作，分类读取和模型已发生。原件实际 IO 也绕过 Gateway ledger。精确位置、完整分级和其他发现以最终报告为准。

必须在原件读取前核对当前员工资格，模型调用前重核，保留后续动作自身授权。Raw 读取必须进入真实 Gateway 的用途授权、EXECUTING、bounded 读取、成功交付与安全错误流程。优先复用已有 inbox.message.evidence.read 模式及真实 Message 绑定；email.inbound.raw.read 面向 review_id，不得伪造 review 借用。若资格用途与既有工具不一致，先说明最小独立插件契约，不能改核心管线或拓宽原 Inbox 角色权限。

模型返回后持久分类之前也须保持当前资格契约，不能撤权后仍写分类；保留原取消/超时传播和投影/模型校验，不把已存在分类或不同tenant的结果当本次成功。员工事实查询沿既有受信system限定lookup，但system自身不授予读取/模型权限。需要契约/接口变更时先记录最小ADR，不放宽根硬边界。

覆盖真实失败场景：配置员工停用/降权后排队 Classify 不读取原件、不调用模型、不保存新的分类；读取后、模型前权限撤销时模型不执行；跨租户或 Message 不匹配拒绝；Gateway 审计失败先于实际读取；正常授权分类仍完成且产生可核 ledger。采用已有受控输入和真实领域/PG/Gateway，模型与对象外部 transport 可作为受控计数端口，不通过预置结果绕开系统。

## M1：受控回复模型连接关闭

infra/controlled/reply_model.py 的四处 with self._connect() 只结束事务，未显式关闭连接。用 closing + 原事务上下文或可靠finally实现确定关闭；保留提交/回滚、固定错误与原调用计数语义。用具名可计数连接验证正常和异常都关闭，不读取原SQLite内容、不新增大规模资源测试。

## 验证与报告

恢复日环境只读预检：.venv Python3.12.14、Docker29.5.3 可用，apps/web/node_modules存在。默认PATH没有node；已核原 `/Users/xueziheng/.nvm/versions/node/v24.15.0/bin/node` 为v24.15.0，同目录有npm/npx。测试子进程临时前置该bin目录即可，不修改用户shell配置。优先保持原验证版本，不使用另一个已发现的bundled24.19.0替换它。

先补能击中旧实现的失败回归，再最小修复。现有覆盖位置包括 tests/unit/test_reply_runtime_binding.py、test_reply_content_boundary.py、test_reply_current_evidence.py，以及 tests/integration/test_reply_completion.py、test_scheduler_reply_trigger.py、test_reply_qualification_workflow.py；按实际改动选择完整受影响文件，不机械全仓重跑。

生产装配变更后需复验 tests/e2e/test_web_core_controlled.py 的原真实受控主链；保留独立审批、验证联系人、未知发送/重放和精确清理。其它新增发现的验证范围随最终清单确定。

改代码后运行结构自检、受影响 Ruff/mypy、敏感扫描与 diff-check。完整后端 9318 / Web 411 是历史版本证据，不能称本修复 HEAD 全仓同轮绿色；只有新增组合风险才请求控制者裁定扩验范围。

精确源码和报告分开提交。完整报告写本目录 final-fix-report.md：每项处置、真实命令/结果/耗时与源码版本、失败历史、owner 清理安全证据、未覆盖边界。返回状态、源码/报告 SHA、测试简表及残余问题。不要把完成实现写成最终复审已通过。
