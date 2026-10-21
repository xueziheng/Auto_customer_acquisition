# ADR 0117：普通邮件持续对话的独立授权与精确关联

日期：2026-10-22。状态：实施中，不代表生产发送获批。

## 决策

持续普通问答复用精确获批 Campaign 的独立 ordinary_dialogue_policy，缺省关闭。
首次普通询盘可由真人选择同一精确范围准入，无需冷 Enrollment；已回复冷 Enrollment
保持终态。Task4 的 prepare_and_enroll 与内部 confirmed_refs 不授予对话或披露权限。

普通对话范围只允许当前确认产品的客户视图，显式负责人、处理依据、有效期、有限轮次、
模型调用次数、输入输出上限及无进展上限。服务记录真实确认人/时间，非空范围参与原审批包，
空字段不进入旧 canonical 包。范围改变沿原 Campaign 新版本重新审批，不修改旧批准记录。

后续实施通过独立 durable binding 连接首次准入或冷发送祖先与 canonical Message、
account/contact/hypothesis/Need；入站只接受精确 SENT 对话头，不模糊匹配、不伪造 Enrollment。
执行按 tenant+source message 唯一，独立 lease/fencing，不冒充人工 ReplyWork handle。
计划存 artifact，账本只元数据；外调前持久调用身份，未知只核对。

所有发送经过新增 HIGH/customer_outbound 插件和原六阶段。模型、凭证、外部连接保持现有
平台与 Gateway 边界。无 Opportunity 的商业问题暂停并进入 owner REVIEW_REPLY；已有机会
复用接管，具体价格/交期/认证等承诺逐次走原审批。分类/需求证据只来自客户原文。

## 影响与验收

修改 Campaign 公共契约与版本持久化，不改九条硬边界。旧包兼容和新范围改版必须回归；
首次准入、新 Message-ID 同 Need、重复/重启/未知/撤权/隔离、真实 scheduler 与协议替身
全链验收通过才可报告 Task5 完成。该决策不构成真实 Campaign 或邮件许可。
