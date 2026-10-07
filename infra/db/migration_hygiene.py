"""迁移版本目录的文件系统卫生检查。"""

from __future__ import annotations

from pathlib import Path
from struct import Struct

_APPLEDOUBLE_MAGIC = b"\x00\x05\x16\x07"
_APPLEDOUBLE_VERSION = 0x00020000
_APPLEDOUBLE_HEADER = Struct(">II16sH")
_APPLEDOUBLE_ENTRY = Struct(">III")


class UnexpectedMigrationSidecarError(RuntimeError):
    """迁移目录出现同名形态但无法证明是 AppleDouble 的文件。"""


def _is_supported_appledouble(sidecar: Path) -> bool:
    """验证 macOS 产生的完整 AppleDouble v2 头及其条目范围。"""
    try:
        data = sidecar.read_bytes()
    except OSError as error:
        raise RuntimeError(f"无法读取迁移 sidecar：{sidecar}") from error

    if len(data) < _APPLEDOUBLE_HEADER.size:
        return False
    magic, version, _filler, entry_count = _APPLEDOUBLE_HEADER.unpack_from(data)
    if magic != int.from_bytes(_APPLEDOUBLE_MAGIC, byteorder="big"):
        return False
    if version != _APPLEDOUBLE_VERSION:
        return False

    entries_end = _APPLEDOUBLE_HEADER.size + entry_count * _APPLEDOUBLE_ENTRY.size
    if entries_end > len(data):
        return False
    for offset in range(_APPLEDOUBLE_HEADER.size, entries_end, _APPLEDOUBLE_ENTRY.size):
        _entry_id, data_offset, data_length = _APPLEDOUBLE_ENTRY.unpack_from(data, offset)
        if data_offset < entries_end or data_offset + data_length > len(data):
            return False
    return True


def remove_appledouble_version_sidecars(versions_directory: Path) -> tuple[Path, ...]:
    """仅删除可由 AppleDouble 魔数确认的迁移版本 sidecar。

    T7 等非原生 macOS 卷可能把资源叉暴露成 ``._*.py`` 文件；Alembic 会把它们当作
    Python 迁移载入并因 NUL 字节失败。文件名相似但不具 AppleDouble 魔数的文件一律
    保留并中止，避免用迁移预处理掩盖或删除真实版本文件。
    """
    recognized: list[Path] = []
    unexpected: list[Path] = []
    for sidecar in sorted(versions_directory.glob("._*.py")):
        if not _is_supported_appledouble(sidecar):
            unexpected.append(sidecar)
            continue
        recognized.append(sidecar)
    if unexpected:
        files = ", ".join(str(path) for path in unexpected)
        raise UnexpectedMigrationSidecarError(
            f"拒绝删除无法确认的迁移 sidecar：{files}"
        )
    for sidecar in recognized:
        sidecar.unlink()
    return tuple(recognized)
