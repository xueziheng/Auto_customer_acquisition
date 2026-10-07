# connectors/whatsapp/ —— WhatsApp（Phase 3，未实现）

仅 opt-in 场景（客户扫码、网站点击、表单同意、邮件转移、展会授权、既有客户）。官方 Business Messaging Policy：主动发起用获批模板、24 小时窗口内才可自由回复、必须提供转真人路径。

**每条发送必须能指向 consent_records 里的一条 opt-in 记录，否则 tool_gateway 拒绝。** 冷启动群发在任何 Phase 都不做。

**当前无代码。**
