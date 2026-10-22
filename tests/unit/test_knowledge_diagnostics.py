"""诊断仅允许固定阶段及受信编号，禁止正文/凭证/异常进入日志。"""
import logging

import pytest

from apps.scheduler_worker.knowledge_diagnostics import (
    LOGGER_NAME,
    MESSAGE,
    KnowledgeDiagnosticFilter,
)


@pytest.mark.parametrize("stage", ["model_identity", "response_schema", "source_evidence", "output_guard", "processing_validation"])
def test_safe_diagnostic(stage):
    record = logging.LogRecord(LOGGER_NAME, logging.WARNING, "", 0, MESSAGE,
                               (stage, "tn_test", "run_test"), None)
    assert KnowledgeDiagnosticFilter().filter(record)


@pytest.mark.parametrize("message,args,exc", [
    ("private document", (), None),
    (MESSAGE, ("private text", "tn_test", "run_test"), None),
    (MESSAGE, ("source_evidence", "tn_test\nsecret", "run_test"), None),
    (MESSAGE, ("source_evidence", "tn_test", "run_test"), (ValueError, ValueError("secret"), None)),
])
def test_unsafe_diagnostic_is_dropped(message, args, exc):
    record = logging.LogRecord(LOGGER_NAME, logging.WARNING, "", 0, message, args, exc)
    assert not KnowledgeDiagnosticFilter().filter(record)
