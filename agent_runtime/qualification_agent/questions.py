"""原需求缺项选择器的固定英文措辞；不包含价格或交期承诺。"""

from shared.errors import ValidationError

_QUESTIONS = {
    "product_category": "Which type of product are you looking for?",
    "application": "Could you share the intended application?",
    "size_spec": "Could you share the approximate size range or specifications?",
    "quantity": "What quantity do you need?",
}


def render_questions(topics: tuple[str, ...]) -> tuple[str, ...]:
    """只呈现原选择器支持的最多两个主题，未知缺项不发明业务问题。"""
    if len(topics) > 2 or any(topic not in _QUESTIONS for topic in topics):
        raise ValidationError("下一问主题不可用")
    return tuple(_QUESTIONS[topic] for topic in topics)
