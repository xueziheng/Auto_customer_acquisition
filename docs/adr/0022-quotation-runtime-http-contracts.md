# ADR 0022：报价准备公共契约与运行时 HTTP 接线

日期：2026-08-29。状态：按已批准 T8B2 brief 实施；各切片验收以实施报告为准。

## 决策

准备页面与正式报价 context 是不同用途。新增共享只读
`NeedQuotePreparationAssessment`，只包含真实需求/hash 绑定和固定缺项状态；
分类实现唯一归 demand，quotation 通过 workflow adapter 调公开服务取得结果。
原数量 hash、完整事实 hash、单位有效性函数与正式 context 的语义保持不变。
缺失数量没有数量 hash；历史零值保留真实 hash 但不能报价；损坏事实失败关闭，
不得被伪装成普通待补字段。

后续安全 HTTP 投影只列明白名单字段，Provenance 摘要不包含原文、URL 或定位；
原件读取依然独立鉴权。成本角色 C、单位用途 U（C 与机会权限交集）、客户文件 F
互不替代。所有新配置显式输入，域、来源、文件组按已批准 brief 分组装配，
唯一审批服务和完整 handlers 在实际 engine 构造前形成。

安全价格依据集合先执行当前C权限，再用成本UoW同session的
`CostingOpportunityReferenceReader.exists`读取tenant+opportunity的真实SELECT EXISTS，
仅返回bool，不展开客户/owner/Need或调用CRM授权。真实存在但无依据返回空集合；
缺机会/跨租户使用新增`CostingQuoteNotFoundError(record_not_found)`固定中文
“成本报价记录不存在”；存储故障不能伪装缺项。旧读取/确认错误语义不迁移。
coverage可按原确认hash恢复，不以并发新latest取代回执；scope按确认时间/ID发现历史。

U授权独立组合原C与机会读取矩阵（当前只有boss交集），员工→机会SHARE租约
不要求owner或issuer、不提前锁Need；内层真实Need事务结束后才释放授权锁。
typed摘要保留原金额、字段可空性、真实规格及完整hash；不增加默认事实。

## 边界

不增加 Phase，不启用发送，不改变金额/置信度/租户/审批硬边界，不修改 Gateway
核心或旧工作流解释。当前文档记录决策，不声明四切片已完成或生产已部署。
