"""当前进程统一联系方式指纹，与API原域标签一致。"""
from domains.prospecting.service import ContactValueHasher
from tool_gateway.fingerprint import HmacFingerprintProvider


class DomainSeparatedContactValueHasher(ContactValueHasher):
    """复用 scheduler HMAC key，但以固定域标签隔离联系方式指纹。"""

    def __init__(self, provider: HmacFingerprintProvider) -> None:
        self._provider = provider

    def fingerprint(self, canonical_value: str) -> str:
        digest, _version = self._provider.fingerprint(
            (b"contact-value-v1", canonical_value.encode("utf-8"))
        )
        return digest
