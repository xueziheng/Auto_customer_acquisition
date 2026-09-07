import os
import sys
from pathlib import Path
import pytest
ROOT=Path('/Volumes/T7/Company/Auto_customer_acquisition/.worktrees/web-core-completion')
sys.path.insert(0, str(ROOT))
os.environ['PYTHON_DOTENV_DISABLED']='1'
os.environ['TRADEOS_REQUIRE_E2E']='1'
class OwnedEvidence:
    def pytest_collection_modifyitems(self, items):
        for item in items:
            if item.path.name == 'test_web_core_controlled.py':
                item.module.EVIDENCE=ROOT/'output/acceptance/final-fix-20260907/e2e'
raise SystemExit(pytest.main(['tests/e2e/test_web_core_controlled.py','-q','--tb=no'], plugins=[OwnedEvidence()]))
