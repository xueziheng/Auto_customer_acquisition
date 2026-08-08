"""Skill Router —— 按 trigger 选技能，一次任务只加载所需。

不把所有 prompt 塞进系统提示词：上下文是稀缺资源，也是行为
可控性的来源——加载了什么技能，Agent 就只有什么能力。

manifest 规范见 ``skills/manifests/schema.yaml``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SkillManifest:
    """技能 manifest 的运行时形式（从 YAML 加载）。

    字段与 ``skills/manifests/schema.yaml`` 对应：
        skill_id, version, domain
        triggers:          什么事件/任务类型激活它
        inputs / outputs:  形状声明
        allowed_tools / blocked_tools
        evidence_required: 输出必须携带的证据字段
        risk_level, cost_class
        freshness:         证据新鲜度窗口（覆盖默认 7 天）
        prompt_ref:        prompt 资产路径（内容不进 manifest）
        eval_refs:         对应的评估集样本
    """

    skill_id: str
    version: str
    domain: str
    triggers: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    evidence_required: tuple[str, ...]
    risk_level: str
    cost_class: str
    prompt_ref: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    freshness_days: int = 7
    eval_refs: tuple[str, ...] = ()


@runtime_checkable
class SkillRouter(Protocol):
    """技能路由。"""

    def load_registry(self, skills_dir: str) -> int:
        """启动时扫描 ``skills/`` 加载全部 manifest，返回数量。

        实现要求：
        - manifest 不合 schema 直接启动失败——坏 manifest 上线后
          的表现是「某类任务静默没有技能可用」，很难查
        - 同 skill_id 多版本取最高版本，旧版本保留可指定
        """
        ...

    def select(
        self, trigger: str, *, max_skills: int = 3
    ) -> list[SkillManifest]:
        """按 trigger 选技能。

        实现要求：
        - 精确匹配 trigger，不做语义猜测——技能加载是权限边界的
          一部分，「猜着加载」等于「猜着授权」
        - 超过 ``max_skills`` 说明 trigger 设计太宽，记警告并取
          最具体的几个
        - 无匹配返回空列表，由调用方决定降级或转人工，
          不要兜底加载「万能技能」
        """
        ...

    def get(self, skill_id: str, version: str | None = None) -> SkillManifest:
        ...
