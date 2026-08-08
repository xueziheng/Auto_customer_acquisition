# connectors/wecom/ —— 企业微信（Phase 2，未实现）

通知渠道 + 老板指令入口（企业微信里直接说"暂停美国 Campaign"）。可经 OpenClaw 企业微信插件对接（支持私聊、群聊、主动消息、文件、访问控制）。

密钥引用 `WECOM_SECRET_REF`。凭证铁律同 base.py。指令入口只产生 DirectiveProposal，确认仍在系统内完成——**不允许在聊天里"顺手确认"绕过预计行为展示**。

**当前无代码。**
