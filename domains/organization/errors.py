"""组织域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation


class PlaybookNotConfiguredError(PolicyViolation):
    """Playbook 未配置。

    排除品类和金额底线是老板的商业决策，代码不提供默认值。
    探索、打分、Campaign 在 Playbook 配置前一律拒绝启动。
    """
