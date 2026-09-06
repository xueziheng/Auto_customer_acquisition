# Task9 独立审查上下文

Fix1 I2补充裁定：首次review称FastAPI422，实际Sourcing原RequestValidationError被全局与业务ValidationError统一映400；因此不得将所有400作为确定未提交。controller已具名核middleware250/275–281、Settings70–93局部422和Sourcing445/命令模型，授权仅reconcile请求模型验证失败显式脱敏ApiErrorResponse 422/application调用0。可机械提取原Settings显式422 route处理至同API公共位置共享，Settings行为不变；业务400/运行时PydanticError/此前未知仍冻结，原全局错误映射不改。Fixdiff的新增HTTP契约、生成类型与真实校验测试属于此裁定范围，原review错误前提已更正但输入不能修正缺陷仍需解决。

以task-9-brief.md、正式子规格和task-9-report.md为需求与执行证据；controller派发提供精确diff。Task8已经3ae0f1e独立Approved，Task9基点91c77754e295f5eb9754bd87e4e4f920985ae2fe。本文件不是结论。

任务是失败/暂停/恢复语义与真实已有命令，不新增通用幂等账本、发送重试或状态绕过。Sourcing原HTTP路由仅校验header，实际canonical命令核reconciliation_id/完整payload；Settings原任意HTTP便清key/body不能当提交未知安全恢复。不同对象/身份/版本不得串用请求意图，目标消失不能自动选首项应用旧证据。授权拒绝须共享gate失效而非仅清DOM，8刚修过跨通道旧响应复活。

原发件身份管理/登记/预热已有8真实浏览器证据，当前显示remaining/target已修，不重做。原入站retry只原位expected_version，429/暂态期限不得提前抹去，history过期不能重置最新或称已恢复。没有实际合法命令的状态如实待核对，不提供虚假按钮；费用/预算未知不能0化。

实施中具名发现原Sourcing list_uncertain_execution_read_views只在canonical不存在时can=true；reconcile_uncertain原命令却先保存canonical，再ack quota/deliver_event，支持uncertain/consumed恢复。Controller核两方法后允许本批增加最小后端安全action投影（先spec/ADR），复用原命令/同一不可变payload与reconciled_by。须核当前权限、精确tenant/case/run/execution/canonical、active public_search/quota及必要原has_delivered_event，错误不开放，POST仍最终重验；已送达仅恢复事件已交付，不当作业务完成。无新通用账本/搜索/核对事实，不让前端覆盖旧can=false。报告需保存后ack失败、ack后交付失败、精确同键恢复/旧run/错actor/重放的实际证据。

审查只读，不派子代理；完整diff一次或顺序chunks，截断hunk才补文件，域外只一次具名风险定向检查并注明。不要为确认report重复跑同版本测试；具体未覆盖风险才定向验证。结论含Spec与Task quality、具名分级问题/行号及Cannot verify。Task12仍统一全量/120warnings实际归属，13正式持久化；当前Git stderr只捕获不维修。

## Global Constraints

- 根 `AGENTS.md` 的九条硬边界和逐次人工审批要求保持不变；进入实现目录前读所有生效的就近规则。
- 本轮先交付本机受控 Web；不实现 Tauri，不开展真实客户发送、供应商联系、采购或部署。
- 金额只用 Decimal；模型不输出最终金额和概率；事实、推断及客户表达结构分离。
- tenant、actor、scope、Provenance、审批结论、预算不能来自客户端自报或测试绕过。
- 保持 `apps → workflows / agent_runtime → domains → shared`；进程之间不得互导。
- 新外部工具使用 manifest + check + handler 插件；不往 Gateway 核心增加具体业务分支。
- 已有 Catalog 培养 Case 仍止于 queued；本轮不新增培养消费者或默认生产策略。
- 每批次保留有意义的失败测试与聚焦通过证据；全量测试在集成里程碑执行，未改代码且无新疑点时不重复跑。
- 所有新文档用中文；API 类型从 OpenAPI 生成，前端不手写重复业务 DTO。
- 本计划中的“新增路径/接口”为拟议实现，不表示文件或能力已经存在。未来批次先产出该批详细规格，再按测试推进，不凭这份总计划猜测跨域契约。

Ruling: Task9新操作HTTP header固定由reconciliation_id派生为sourcing-reconcile-{reconciliation_id}，初次、同页与刷新恢复保持一致；legacy原随机header未持久化且路由只校验后丢弃，允许仅在后端明确resume、当前同actor且canonical命令字段完整时，从既有canonical派生稳定header续交付。— 实际耐久保证是原reconciliation_id/完整payload/reconciled_by，无法凭空恢复旧随机header，新增浏览器账本没有权威性；这是对原header字面要求的兼容裁定，不声称HTTP header持久幂等。— 保留现有header校验，不新建业务命令或核对事实；代价是legacy网络header与最初值不同，须以新操作刷新同key、legacy canonical恢复不重复事实/额度/事件的定向证明和正式spec/ADR说明约束。
