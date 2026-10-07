"""B2非秘密配置测试值；不是部署默认，不接真实Provider。"""

from tests.unit.test_evidence_parser_client import probe_limits
from tests.unit.test_evidence_text_profiles import parse_limits


def quotation_settings_values():
    """Linux解析预算沿A已验收fixture，数据库/文件预算显式独立。"""
    read = {
        "connect_timeout_ms": 1000,
        "read_timeout_ms": 1000,
        "total_timeout_ms": 5000,
        "chunk_bytes": 65536,
        "maximum_attempts": 1,
    }
    return {
        "core": {
            "lock_timeout_ms": 250,
            "statement_timeout_ms": 2000,
            "maximum_page_size": 20,
            "expiry_batch_limit": 10,
        },
        "evidence": {
            "raw_maximum_bytes": 2097152,
            "object_read": dict(read),
            "parser": parse_limits().model_dump(),
            "probe": probe_limits().model_dump(),
        },
        "files": {
            "template_version": "quote_pdf_v1",
            "maximum_bytes": 2097152,
            "maximum_pages": 8,
            "maximum_text_bytes": 262144,
            "object_read": dict(read),
            "object_write": {
                "connect_timeout_ms": 1000,
                "read_timeout_ms": 1000,
                "total_timeout_ms": 5000,
                "maximum_attempts": 1,
            },
            "rate_limit": {
                "maximum_admissions": 3,
                "window_seconds": 60,
                "lock_timeout_ms": 250,
                "statement_timeout_ms": 2000,
            },
            "gateway_lease_seconds": 120,
        },
    }
