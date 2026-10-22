"""中性回复核对结果复用原分类、纠正与 owner 工作队列；不新建业务表。"""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None

_CATEGORIES = (
    "'clear_interest','willing_to_continue','requests_materials','requests_quote',"
    "'requests_sample','provides_specification','no_current_need','future_need_possible',"
    "'refers_other_contact','rejection','unsubscribe','bounce','auto_reply','complaint'"
)
_ACTIONS = "'start_qualification','mark_future_restart','create_follow_up','intake_new_contact'"
_QUEUES = (
    "(action = 'start_qualification' AND owner_queue = 'need_qualification') OR "
    "(action = 'mark_future_restart' AND owner_queue = 'future_restart_review') OR "
    "(action = 'create_follow_up' AND owner_queue = 'follow_up') OR "
    "(action = 'intake_new_contact' AND owner_queue = 'verified_contact_intake_review')"
)


def _replace(extended: bool) -> None:
    categories = _CATEGORIES + (",'needs_review'" if extended else "")
    actions = _ACTIONS + (",'review_reply'" if extended else "")
    queues = _QUEUES + (
        " OR (action = 'review_reply' AND owner_queue = 'reply_review')" if extended else ""
    )
    for table, name, expression in (
        ("conversation_classifications", "ck_conversation_classifications_category", f"category IN ({categories})"),
        ("conversation_classification_corrections", "ck_conversation_classification_corrections_category", f"corrected_category IN ({categories})"),
        ("conversation_reply_work", "ck_conversation_reply_work_action", f"action IN ({actions})"),
        ("conversation_reply_work", "ck_conversation_reply_work_queue", queues),
    ):
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, expression)


def upgrade() -> None:
    _replace(True)
    op.create_check_constraint(
        "ck_conversation_classifications_review_fields",
        "conversation_classifications",
        "category <> 'needs_review' OR candidate_fields = '[]'::jsonb",
    )


def downgrade() -> None:
    # 全表 schema 回退保护，不读取客户内容；锁避免核对记录在检查后被插入。
    op.execute("LOCK TABLE conversation_classifications, conversation_classification_corrections, conversation_reply_work IN ACCESS EXCLUSIVE MODE")
    for table, predicate in (
        ("conversation_classifications", "category = 'needs_review'"),
        ("conversation_classification_corrections", "corrected_category = 'needs_review'"),
        ("conversation_reply_work", "action = 'review_reply'"),
    ):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table} WHERE {predicate})")):
            raise RuntimeError("已存在回复核对记录，拒绝丢弃记录或回退分类契约")
    op.drop_constraint("ck_conversation_classifications_review_fields", "conversation_classifications", type_="check")
    _replace(False)
