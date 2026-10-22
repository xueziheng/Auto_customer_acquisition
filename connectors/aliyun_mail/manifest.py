"""阿里企业邮箱的独立注册元数据。"""

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="aliyun_mail",
    capabilities=("email.send",),
    secret_refs=("ALIYUN_MAIL_CLIENT_PASSWORD_REF",),
    rate_limit_note="单封 SMTP 调用不自动重试；不确定结果只读对账",
    compliance_note="固定 TLS 端点；客户发送由原 Gateway 六阶段授权，必须含退订链接",
)
