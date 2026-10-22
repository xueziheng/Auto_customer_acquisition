"""仅将固定知识库诊断写入本机系统日志，不转发资料和异常正文。"""
from __future__ import annotations

import logging
import re
from logging.handlers import SysLogHandler
from pathlib import Path

LOGGER_NAME = "workflows.enterprise_knowledge.processor"
MESSAGE = "enterprise_knowledge_validation_failed stage=%s tenant=%s job=%s"
STAGES = frozenset({
    "model_identity", "response_schema", "source_evidence", "output_guard",
    "response_schema_limit", "response_schema_missing", "response_schema_extra",
    "response_schema_json", "response_schema_value",
    "response_schema_limit_facts", "response_schema_limit_title",
    "response_schema_limit_label", "response_schema_limit_value",
    "response_schema_limit_source_quote", "response_schema_limit_inferences",
    "processing_validation",
})


class KnowledgeDiagnosticFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return (
            record.name == LOGGER_NAME and record.msg == MESSAGE
            and record.exc_info is None and record.stack_info is None
            and isinstance(args, tuple) and len(args) == 3
            and isinstance(args[0], str) and args[0] in STAGES
            and all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)
                    is not None for value in args[1:])
        )


def configure_knowledge_diagnostics() -> None:
    """Linux 本机 journal 接收独立白名单日志；不更改通用 stdout/stderr 策略。"""
    logger = logging.getLogger(LOGGER_NAME)
    if any(handler.get_name() == "tradeos-knowledge-safe" for handler in logger.handlers):
        return
    if not Path("/dev/log").is_socket():
        return
    handler = SysLogHandler(address="/dev/log")
    handler.set_name("tradeos-knowledge-safe")
    handler.ident = "tradeos-knowledge: "
    handler.addFilter(KnowledgeDiagnosticFilter())
    logger.addHandler(handler)
