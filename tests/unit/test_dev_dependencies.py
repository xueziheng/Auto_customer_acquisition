"""CI 测试依赖必须由 dev extra 显式声明。"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


def test_dev_extra_declares_httpx_used_by_api_tests() -> None:
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    dependencies = config["project"]["optional-dependencies"]["dev"]
    names = {re.split(r"[<=>!~;\[]", value, maxsplit=1)[0] for value in dependencies}
    assert "httpx" in names
