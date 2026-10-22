"""QQ邮箱连接器元数据。"""
from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(connector_id="qq_mail", capabilities=("email.send",),
    secret_refs=("QQ_MAIL_AUTHORIZATION_CODE_REF",),
    rate_limit_note="单封SMTP不自动重试；未知结果仅对账",
    compliance_note="固定TLS端点；发送必须经Gateway与联系人验证")
