"""采集链跨Connector、Gateway和持久账本的固定诊断投影，无业务正文。"""

from dataclasses import dataclass

STAGES = frozenset(
    {
        "page_http", "page_robots", "browser_url", "browser_init", "browser_nav",
        "browser_html", "browser_shot", "browser_trc", "browser_end", "browser_ipc", "browser_art",
        "request",
        "provider",
        "response",
        "tool_args",
        "tool_exec",
        "context",
        "final",
        "final_model", "final_shape", "final_role", "final_absent", "final_status",
        "final_parts", "final_phase", "final_mixed", "final_empty", "final_size",
        "final_fence", "final_json", "final_object", "final_number",
        "usage",
        "budget",
        "cli_exit",
        "cli_no_final",
        "cli_match",
        "source_scope",
        "source_order",
        "page_scope",
    }
)
CODES = {
    "authentication": "auth",
    "insufficient_balance": "balance",
    "invalid_request": "req",
    "rate_limit": "rate",
    "provider_error": "server",
    "invalid_response": "res",
    "output_limit": "limit",
    "unknown": "unknown",
}


@dataclass(frozen=True)
class DiscoveryDiagnostic:
    """只接受固定阶段/分类和0..16轮，兼容既有30字符outcome列。"""

    stage: str
    code: str
    rounds: int

    def __post_init__(self) -> None:
        if (
            self.stage not in STAGES
            or self.code not in CODES
            or type(self.rounds) is not int
            or not 0 <= self.rounds <= 16
        ):
            raise ValueError("采集诊断无效")

    @property
    def outcome(self) -> str:
        return f"failed:{self.stage}:{CODES[self.code]}:{self.rounds}"


def is_discovery_failure(value: str) -> bool:
    """仓储只允许有限诊断组合，不接受自由文本。"""
    parts = value.split(":")
    return (
        len(parts) == 4
        and parts[0] == "failed"
        and parts[1] in STAGES
        and parts[2] in CODES.values()
        and parts[3] in {str(i) for i in range(17)}
    )


class DiscoveryDiagnosticError(ValueError):
    """跨工具会话保留安全投影，不携带原始异常。"""

    def __init__(self, diagnostic: DiscoveryDiagnostic) -> None:
        super().__init__("采集工具被拒绝")
        self.diagnostic = diagnostic
