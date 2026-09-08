# 负责人提醒审查上下文

目标：只在待接管时每7200秒提醒当前员工，接受提交后不再产生新提醒，保持归属；受众/自然UTC时间是控制者已明示假设，见progress.md。
需求：task-1-brief.md 与 docs/superpowers/specs/2026-09-08-owner-handoff-reminders-design.md。
核心风险：接受提交与旧Outbox/已排队通知的竞争；当前owner/员工状态；幂等轮次；重启与模式版本兼容；配置实际贯通pilot/API/scheduler/站内存储。
不把此前本机pilot完整验收或历史全仓计数算作本次通过。不重复运行实现者在同代码已跑的覆盖测试；审查核对报告中的命令/结果/源码版本及断言辨别力。有具体未回答风险才追加窄probe。
不读或输出凭证、DSN、原始异常、私有profile、历史output；Git stderr捕获只报数量。根/就近AGENTS必读；不修改源码或index/HEAD，不派子代理。
主动退回、Agent真实接续、金额分档关闭仍未实现；本次不得把它们写成完成。真实profile缺其他商业政策，不能代用户填默认。
输出Task spec compliance和quality双结论，精确file:line及severity，区分事实与风险、测试记录与审查者运行。
