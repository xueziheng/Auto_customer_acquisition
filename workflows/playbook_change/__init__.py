"""Company Playbook 审批工作流公共装配入口。"""

from workflows.playbook_change.flow import (
    build_playbook_change_definition,
    build_playbook_change_handlers,
    register_playbook_change,
)

__all__ = (
    "build_playbook_change_definition",
    "build_playbook_change_handlers",
    "register_playbook_change",
)
