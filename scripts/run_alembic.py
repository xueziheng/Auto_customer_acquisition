"""受控 Alembic 入口：先清除 macOS AppleDouble 迁移 sidecar，再执行命令。"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import main as alembic_main

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from infra.db.migration_hygiene import remove_appledouble_version_sidecars

_VERSIONS_DIRECTORY = _REPO_ROOT / "migrations" / "versions"


def main(argv: list[str] | None = None) -> None:
    """在调用 Alembic 前安全清除已确认的 AppleDouble sidecar。"""
    removed = remove_appledouble_version_sidecars(_VERSIONS_DIRECTORY)
    if removed:
        paths = ", ".join(str(path.relative_to(_REPO_ROOT)) for path in removed)
        print(f"已清除 AppleDouble 迁移 sidecar：{paths}", file=sys.stderr)
    alembic_main(argv=argv, prog="alembic")


if __name__ == "__main__":
    main(sys.argv[1:])
