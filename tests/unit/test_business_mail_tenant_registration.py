"""冷进程必须独立注册普通来信四表，不能依赖其他测试或API先导入业务仓储。"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BUSINESS_TABLES = {
    "business_mailboxes",
    "business_mail_messages",
    "business_mail_receipts",
    "business_mail_classifications",
}


def registered_tables(*, warm: bool) -> set[str]:
    script = """import json,sys
assert 'infra.db.business_mail_tables' not in sys.modules
"""
    if warm:
        script += "import infra.db.business_mail_tables\n"
    script += """from infra.db.tenant_security import _tenant_table_names
print(json.dumps(sorted(_tenant_table_names())))
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, "独立进程ORM注册失败"
    return set(json.loads(result.stdout))


def test_cold_process_registers_business_mail_without_application_import_order():
    cold = registered_tables(warm=False)
    warm = registered_tables(warm=True)
    assert BUSINESS_TABLES <= cold, "冷进程遗漏普通来信四表"
    assert cold == warm, "租户表集合不能随业务模块导入顺序变化"
