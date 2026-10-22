"""生产全局日志关闭时，仍只输出白名单阶段诊断。"""
import logging

from apps.scheduler_worker.knowledge_diagnostics import (
    LOGGER_NAME,
    KnowledgeDiagnosticFilter,
)
from shared.errors import ValidationError
from tests.unit.test_enterprise_knowledge_processor import setup_driver


async def test_fixed_diagnostic_survives_global_disable_without_enabling_logging():
    driver, service, _, _, analyzer, _ = setup_driver()
    analyzer.analyze.side_effect = ValidationError(
        "private source must not be logged", context={"knowledge_stage": "source_evidence"},
    )
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    handler.addFilter(KnowledgeDiagnosticFilter())
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(handler)
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        await driver.scan_once()
        assert logging.root.manager.disable == logging.CRITICAL
        logger.warning("private source must not be logged")
        assert records == ["enterprise_knowledge_validation_failed stage=source_evidence tenant=tn_test job=run_job"]
        assert service.fail_processing.await_args.kwargs["reason"] == "invalid_analysis"
        service.complete_processing.assert_not_called()
        analyzer.analyze.assert_awaited_once()
    finally:
        logger.removeHandler(handler)
        logging.disable(previous)
