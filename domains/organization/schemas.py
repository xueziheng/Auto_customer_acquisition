"""组织域对外 DTO。（浅域：直接复用 models 的 Playbook，暂无独立 DTO）

需要对外裁剪时（例如给员工看的 Playbook 摘要不含预算），在此添加
View 类，不要直接暴露实体。
"""

from __future__ import annotations
