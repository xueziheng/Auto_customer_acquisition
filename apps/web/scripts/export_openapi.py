"""从固定的 API 工厂导出确定性 OpenAPI JSON 到标准输出。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
APPS_ROOT = REPOSITORY_ROOT / "apps"


def export_openapi() -> dict[str, Any]:
    """调用零参数 API 工厂并返回不含运行时依赖的 OpenAPI 契约。"""
    sys.path.insert(0, str(REPOSITORY_ROOT))
    from apps.api.main import create_app

    return create_app().openapi()


if __name__ == "__main__":
    json.dump(export_openapi(), sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
