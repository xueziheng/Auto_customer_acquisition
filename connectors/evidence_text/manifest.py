"""离线能力声明，不改变通用ConnectorRegistry。"""

from connectors.base import ConnectorManifest

manifest = ConnectorManifest(
    connector_id="evidence_text",
    capabilities=("evidence.pdf_text_v1", "evidence.rfc822_plain_v1"),
    secret_refs=(),
    rate_limit_note="Linux资源探针门控，按显式并发/队列/CPU/内存/墙钟预算执行。",
    compliance_note="纯离线取证；无网络、OCR、业务授权或金额单位推断。",
)
