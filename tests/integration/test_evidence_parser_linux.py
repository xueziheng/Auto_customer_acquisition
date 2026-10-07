"""宿主只调度真实Linux资源runner，不以Mac skip/mock验收。"""

from tests.integration.quote_evidence_linux_support import run_resource_cases


def test_real_linux_resource_chain():
    code, output = run_resource_cases()
    assert code == 0, output
    assert "passed" in output and "skipped" not in output
    print(output)
