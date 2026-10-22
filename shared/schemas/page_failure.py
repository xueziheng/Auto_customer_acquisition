"""公开页面失败的固定私有投影；无URL、正文或异常原文。"""

from shared.schemas.discovery_diagnostic import DiscoveryDiagnostic

PAGE_STAGES = frozenset(
    {
        "page_http",
        "page_robots",
        "browser_url",
        "browser_init",
        "browser_nav",
        "browser_html",
        "browser_shot",
        "browser_trc",
        "browser_end",
        "browser_ipc",
        "browser_art",
    }
)
PAGE_REASONS = frozenset(
    {
        "http_error",
        "access_denied",
        "login_required",
        "unsafe_redirect",
        "network_unknown",
        "timeout",
        "failed",
        "invalid_frame",
    }
)


class PageReadFailure(ValueError):
    """只有明确HTTP响应或访问拒绝允许换资源；其余失败仍关闭会话。"""

    def __init__(self, stage: str, reason: str) -> None:
        if stage not in PAGE_STAGES or reason not in PAGE_REASONS:
            raise ValueError("公开页面诊断无效")
        super().__init__("公开页面未取得证据")
        self.stage, self.reason = stage, reason

    @property
    def recoverable(self) -> bool:
        return self.reason in {
            "http_error",
            "access_denied",
            "login_required",
            "unsafe_redirect",
        }

    @property
    def diagnostic(self) -> DiscoveryDiagnostic:
        return DiscoveryDiagnostic(
            self.stage,
            "unknown"
            if self.reason in {"network_unknown", "timeout"}
            else "invalid_response",
            0,
        )

    def frame(self) -> dict[str, str]:
        return {"stage": self.stage, "reason": self.reason}
