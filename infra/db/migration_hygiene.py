"""迁移版本目录的文件系统卫生检查。"""

from __future__ import annotations

from pathlib import Path

_APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"


class UnexpectedMigrationSidecarError(RuntimeError):
    """迁移目录出现同名形态但无法证明是 AppleDouble 的文件。"""


def remove_appledouble_version_sidecars(versions_directory: Path) -> tuple[Path, ...]:
    """仅删除可由 AppleDouble 魔数确认的迁移版本 sidecar。

    T7 等非原生 macOS 卷可能把资源叉暴露成 ``._*.py`` 文件；Alembic 会把它们当作
    Python 迁移载入并因 NUL 字节失败。文件名相似但不具 AppleDouble 魔数的文件一律
    保留并中止，避免用迁移预处理掩盖或删除真实版本文件。
    """
    removed: list[Path] = []
    unexpected: list[Path] = []
    for sidecar in sorted(versions_directory.glob("._*.py")):
        try:
            with sidecar.open("rb") as stream:
                is_appledouble = stream.read(len(_APPLEDOUBLE_MAGIC)) == _APPLEDOUBLE_MAGIC
        except OSError as error:
            raise RuntimeError(f"无法读取迁移 sidecar：{sidecar}") from error
        if not is_appledouble:
            unexpected.append(sidecar)
            continue
        sidecar.unlink()
        removed.append(sidecar)
    if unexpected:
        files = ", ".join(str(path) for path in unexpected)
        raise UnexpectedMigrationSidecarError(
            f"拒绝删除无法确认的迁移 sidecar：{files}"
        )
    return tuple(removed)
