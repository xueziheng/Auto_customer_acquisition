"""固定有界的会话状态图。"""

from workflows.engine.runner import StepDefinition, WorkflowDefinition


def build_assistant_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_type="assistant",
        version=1,
        steps=tuple(
            StepDefinition(step_name=s, handler_ref=f"assistant.{s}", max_retries=0)
            for s in (
                "load_context",
                "generate",
                "read",
                "generate_explanation",
                "apply_result",
                "complete",
            )
        ),
        transitions={
            "load_context": ("generate",),
            "generate": ("read", "apply_result"),
            "read": ("generate_explanation",),
            "generate_explanation": ("apply_result",),
            "apply_result": ("complete",),
        },
    )
