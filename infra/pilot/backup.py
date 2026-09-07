"""停止容器的物理卷冷备份；整包先验后恢复新 owner，失败保留安全状态。"""

from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import stat
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

from pydantic import Field

from .config import (
    PilotConfig,
    PilotError,
    StrictModel,
    allocated_port,
    checked_directory,
    exclusive_profile_lock,
    private_read,
    private_write,
)
from .resources import IMAGES, MOUNTS, PilotProfile

FILES = frozenset({"config.json", "database.tar", "objects.tar"})


class BackupManifest(StrictModel):
    version: Literal[1]
    image_ids: dict[Literal["database", "objects"], str]
    files: dict[str, str] = Field(min_length=3, max_length=3)


@contextmanager
def archive_stream(path: Path) -> Iterator[BinaryIO]:
    """私有普通文件流；归档不整体载入内存。"""
    checked_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise PilotError("backup_invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with archive_stream(path) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_archive(path: Path, root: str) -> dict[str, tuple[int, int, int, str]]:
    """仅正常目录/文件、单根、无重复/链接/设备；返回字节与权限证据用于回读核验。"""
    entries: dict[str, tuple[int, int, int, str]] = {}
    try:
        with tarfile.open(path, "r|") as archive:
            for member in archive:
                name = PurePosixPath(member.name)
                if (
                    name.is_absolute()
                    or ".." in name.parts
                    or not name.parts
                    or name.parts[0] != root
                    or name.as_posix() != member.name.rstrip("/")
                    or member.name in entries
                    or not (member.isfile() or member.isdir())
                    or member.uid < 0
                    or member.gid < 0
                    or member.mode & 0o7000
                ):
                    raise ValueError()
                if member.isdir():
                    digest = "directory"
                else:
                    source = archive.extractfile(member)
                    if source is None:
                        raise ValueError()
                    with source:
                        hasher = hashlib.sha256()
                        while chunk := source.read(1024 * 1024):
                            hasher.update(chunk)
                        digest = hasher.hexdigest()
                entries[member.name] = (member.uid, member.gid, member.mode, digest)
        if root not in entries or entries[root][3] != "directory":
            raise ValueError()
        return entries
    except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
        raise PilotError("backup_invalid") from None


def _archive_container(
    profile: PilotProfile, kind: Literal["database", "objects"], path: Path
) -> None:
    profile.require_stopped()
    container = profile.verify(kind)
    stream, _ = container.get_archive(MOUNTS[kind])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as destination:
        for chunk in stream:
            destination.write(chunk)
        destination.flush()
        os.fsync(destination.fileno())
    profile.require_stopped()


def _rename_new(source: Path, destination: Path) -> None:
    """平台原子 no-replace rename，目标在最终瞬间出现也不能覆盖。"""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = libc.renamex_np
        operation.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = operation(os.fsencode(source), os.fsencode(destination), 4)
    elif sys.platform.startswith("linux"):
        operation = libc.renameat2
        operation.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        result = operation(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    else:
        raise PilotError("atomic_publish_unsupported")
    if result != 0:
        raise PilotError("backup_publish_failed")
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _new_destination(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise PilotError("profile_exists")
    for parent in path.parents:
        if parent.is_symlink():
            raise PilotError("configuration_invalid")
    if not path.parent.is_dir():
        raise PilotError("destination_invalid")


def backup_profile(path: Path, destination: Path) -> None:
    """流式私有暂存后原子发布新备份目录；不停止运行中的应用或存储。"""
    _new_destination(destination)
    profile = PilotProfile(path)
    staging: Path | None = None
    try:
        with exclusive_profile_lock(profile.path):
            profile.reload()
            profile.require_stopped()
            staging = Path(
                tempfile.mkdtemp(prefix=".pilot-backup-", dir=destination.parent)
            )
            staging.chmod(0o700)
            private_write(
                staging / "config.json", private_read(profile.path / "config.json")
            )
            for kind in ("database", "objects"):
                _archive_container(profile, kind, staging / f"{kind}.tar")
                validate_archive(staging / f"{kind}.tar", "data")
            manifest = BackupManifest(
                version=1,
                image_ids={k: v.image_id for k, v in profile.config.storage.items()},
                files={name: file_hash(staging / name) for name in FILES},
            )
            private_write(
                staging / "manifest.json", manifest.model_dump_json().encode()
            )
            profile.require_stopped()
            _rename_new(staging, destination)
    except PilotError:
        raise
    except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
        raise PilotError("backup_failed") from None
    finally:
        profile.client.close()


def verify_backup(path: Path) -> tuple[PilotConfig, BackupManifest]:
    """拒绝未知文件、错 SHA/版本/镜像关系，完整验证后才能创建目标。"""
    try:
        checked_directory(path)
        if {item.name for item in path.iterdir()} != FILES | {"manifest.json"}:
            raise ValueError()
        manifest = BackupManifest.model_validate_json(
            private_read(path / "manifest.json")
        )
        if set(manifest.files) != FILES or set(manifest.image_ids) != {
            "database",
            "objects",
        }:
            raise ValueError()
        for name, expected in manifest.files.items():
            if file_hash(path / name) != expected:
                raise ValueError()
        config = PilotConfig.read(path / "config.json")
        if manifest.image_ids != {k: v.image_id for k, v in config.storage.items()}:
            raise ValueError()
        for kind in ("database", "objects"):
            validate_archive(path / f"{kind}.tar", "data")
        return config, manifest
    except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
        raise PilotError("backup_invalid") from None


def _restore_volume(
    profile: PilotProfile, kind: Literal["database", "objects"], archive: Path
) -> None:
    """官方 archive API 显式 copyUIDGID；再对停止卷逐项核对内容、uid/gid/mode。"""
    profile.require_stopped()
    container = profile.verify(kind)
    api = profile.client.api
    with archive_stream(archive) as stream:
        response = api._put(
            api._url("/containers/{0}/archive", container.id),
            params={
                "path": str(PurePosixPath(MOUNTS[kind]).parent),
                "noOverwriteDirNonDir": "true",
                "copyUIDGID": "true",
            },
            data=stream,
            headers={"Content-Type": "application/x-tar"},
        )
        api._raise_for_status(response)
    verification = profile.path / (".restored-" + kind + ".tar")
    try:
        _archive_container(profile, kind, verification)
        if validate_archive(verification, "data") != validate_archive(archive, "data"):
            raise PilotError("restore_volume_mismatch")
    finally:
        verification.unlink(missing_ok=True)


def restore_profile(backup: Path, path: Path) -> PilotProfile:
    """验证完整包后仅创建新资源，保留 tenant/bucket/key；撤销会话后停止交付。"""
    _new_destination(path)
    source, manifest = verify_backup(backup)
    # 本地镜像白名单及确切 ID 都须在创建目录前核验，绝不自动 pull。
    import docker  # type: ignore[import-untyped]

    try:
        with closing(
            docker.DockerClient(base_url="unix:///var/run/docker.sock", timeout=30)
        ) as client:
            for kind, expected in manifest.image_ids.items():
                if (
                    client.images.get(IMAGES[kind]).id != expected
                    or client.images.get(expected).id != expected
                ):
                    raise ValueError()
    except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
        raise PilotError("backup_image_unsupported") from None
    path.mkdir(mode=0o700)
    profile: PilotProfile | None = None
    with exclusive_profile_lock(path):
        owner = secrets.token_hex(16)
        ports: set[int] = {
            source.api_port,
            source.scheduler_port,
            source.notification_port,
        }
        fresh: list[int] = []
        while len(fresh) < 3:
            port = allocated_port()
            if port not in ports:
                ports.add(port)
                fresh.append(port)
        config = source.model_copy(
            update={
                "owner": owner,
                "storage": {},
                "api_port": fresh[0],
                "scheduler_port": fresh[1],
                "notification_port": fresh[2],
                "database_port": 0,
                "object_port": 0,
                "restore_state": "pending",
            }
        )
        config.write(path / "config.json")
        try:
            profile = PilotProfile(path)
            profile.provision_storage_locked(
                {str(k): v for k, v in manifest.image_ids.items()}
            )
            for kind in ("database", "objects"):
                _restore_volume(profile, kind, backup / f"{kind}.tar")
            profile.start_storage_locked(restoring=True)
            profile.check_schema_locked()
            profile.revoke_restored_sessions_locked()
            profile.stop_storage_locked()
            profile.config = profile.config.model_copy(
                update={"restore_state": "complete"}
            )
            profile.save()
            return profile
        except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
            if profile is not None:
                profile.config = profile.config.model_copy(
                    update={"restore_state": "failed"}
                )
                profile.save()
                try:
                    profile.stop_storage_locked()
                    profile.publish_processes_locked(
                        supervisor=None,
                        processes=(),
                        status="failed",
                        reason="restore_failed",
                    )
                except Exception:  # noqa: BLE001 安全边界仅输出固定错误码
                    private_write(
                        path / "restore-failure.json",
                        b'{"reason":"restore_cleanup_unknown"}',
                    )
                profile.client.close()
            raise PilotError("restore_failed") from None
