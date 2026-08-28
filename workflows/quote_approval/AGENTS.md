# workflows/quote_approval/ —— 报价创建与单轮审批流程

## 触发

可信应用调用`start_quote_approval`，先验证当前四成本角色的内部读取/提交权，再创建或恢复真实run。

## 主要状态

```text
assemble → submit → wait → apply → mark_applied → notify → complete
                       └─ 拒绝/过期/旧版本失效 → notify → complete
```

## 关键约束

当前报价ABAC由quotation唯一规则判断，经adapter注入approvals；低底线另需margin_floor_override。
wait进入即poll防早到事件，timeout也重读真实包并关闭本轮，不批准、不自动重提。确定性阻断失败关闭。

## Phase 2 创建与恢复

`QuoteApplicationService`只编排公开端口：先当前四角色权限和完整key/intent重放，再读scope/context，
在context SHARE内进入报价机会锁session并重新查询同key，preflight通过才真实freeze/显式basis映射/quote提交。
完成操作在全部context/quote锁退出后执行，`PersistentQuoteCreationCompletionReader`只读真实持久报价；
未写入报价不得生成成功receipt。quote已提交而complete失败只允许原key恢复，不重冻或增加版本。
旧pending只能原prepared_by继续写，已持久成功可由当前有内部读取权的员工恢复；不扩张CRM或原件权限。

`basis_adapter`逐字段映射完整冻结快照，费用不伪装采购，cost_fx_rates与quote_fx分开；不能重算上游hash。
实际抬头由`PersistentQuoteIssuerReader`读取老板确认版本，不补样例公司。新scope/抬头不改写历史quote。
创建入口不证明审批或T8发送/客户文件授权；不可从纯客户投影或成功创建推断可以对客发送。

## Phase 2 审批持久恢复

`QuoteApprovalApplication`只调用公开端口。部分submit/bind失败复用原namespace/hash/limit及真实包；
所有决定从read_fact转换，不从事件或UI can_decide构造。事件只以approval_id唤醒绑定的原run。
固定initial_context只含quote_id/version/hash/prepared_by/initiated_by，后续仅追加ID/期限/固定状态码；
禁止覆盖quote_version/hash或存内部payload、金额、原文、原始异常。

fresh apply先取得全部真实decider→完整context→报价session→政策租约；报价提交并释放所有租约后
才补记各包APPLIED。真实成功receipt优先；APPROVED/APPLIED混合可重启修复，无receipt不能假成功。
确定性失败重新持报价机会锁先查receipt，有成功便补记，不让迟到apply_failed覆盖成功。

run reader通过延迟闭包取实际engine，完整WorkflowRun只留workflow内部。必须先完成service/application/
handlers/engine构造并发布闭包，再启动worker；未装配默认拒绝。get_run用独立MVCC读取，终态也保留归属。
poll_due/deliver_event跨handler的Run锁为NO KEY UPDATE，允许独立报价事务的真实FK KEY SHARE校验；
执行期间不得改run/tenant/idempotency唯一键。step锁、锁序、cancel及失败收尾锁保持原样。

notifier只接受metadata及稳定幂等键，实际通知与API/worker装配由T8负责；T5不调用外部provider、
不生成PDF/发邮件、不用成功receipt替代当前文件actor及原件ACL。

## Phase 2 安全准备适配

DemandQuotePreparationProjector只调用demand.service的纯评估并转换固定错误，
不复制数量/单位规则、不用未知错误制造普通缺项。CurrentNeedUnitAuthorizer
固定原C与OPPORTUNITY_READ交集，使用真实当前员工role和TENANT scope，
初检与锁内同样核权，不提高角色；原件IO在授权锁外，guard保护到内层事务提交。

`http`仅适配公开服务：scope/calculate的机会由真实costing.get_sheet读取；coverage
确认后按返回hash精确读取，不用latest替代。创建/修订路径必须与原CAS命令一致；
内部响应使用各域纯白名单，原文技术wrapper只能领取本次已授权T8A结果。
QuoteApprovalStarter只返回真实run绑定核验后的quote/run，不把202解释为批准。

运行时当前员工只能经真实QuoteEvidenceContextReader读取；成本仍用原C矩阵，单位仍用原U
交集。CurrentCostScopeSourceAccess只重验来源授权/metadata，不读原文、不复制冻结规则。
DeferredQuoteIssuerReader未发布时固定dependency_unavailable，发布后仅委托真实quotation。
步骤prepared_by/initiated_by按既有fact_identity验证并保留原字符串，不要求emp_前缀，
其他run/version/hash/当前员工/独立审批绑定不放宽。QuoteSendReceiptReader明确失败关闭，
不能用Outreach发送、下载或PDF生成回执自证正式报价发送。
