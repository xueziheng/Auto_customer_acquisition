"""宿主只编排固定Linux+临时PG，无fake/skip替代同链。"""

from tests.integration.quote_evidence_linux_support import run_chain_cases


def test_real_linux_gateway_and_domain_services():
    status, summary = run_chain_cases()
    assert status == 0, summary
    assert "skipped" not in summary
    assert "passed" in summary
    lines = summary.splitlines()
    assert lines.count("source_linux_case_passed=prices_and_unit_history") == 1
    assert lines.count("source_linux_case_passed=clipped_source_rejected") == 2
    print(summary)
