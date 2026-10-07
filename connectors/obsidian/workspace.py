"""企业知识资料的任务只读快照；不扫描已有 vault，不启动模型。

调用者必须先重读员工身份，按企业与客户负责人授权，读取原件并确认可用于当前任务。
本模块只验证传入数据的技术绑定，不能把自行构造的 DTO 当成授权。网页上传、产品发布、
检索、Gateway 记账和 Codex 执行器尚未装配；不能将此模块宣称为已接入网页的资料库。
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_HASH = re.compile(r"[0-9a-f]{64}")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class WorkspaceBoundaryError(ValueError):
    """固定安全错误，不携带内容、原路径或底层异常文本。"""


def _reject() -> WorkspaceBoundaryError:
    return WorkspaceBoundaryError("knowledge_workspace_boundary_rejected")


def _component(value: str) -> None:
    if not isinstance(value, str) or _COMPONENT.fullmatch(value) is None:
        raise _reject()


@dataclass(frozen=True)
class KnowledgeTaskScope:
    """服务端为单次任务固定的身份；不得由 HTTP body 或模型决定。"""

    tenant_id: str
    employee_id: str
    run_id: str

    def __post_init__(self) -> None:
        for value in (self.tenant_id, self.employee_id, self.run_id):
            _component(value)


@dataclass(frozen=True)
class AuthorizedKnowledgeDocument:
    """已授权的 UTF-8 Markdown 文本；源文件名只供校验，永不用来拼接路径。

    上游负责产品发布状态、字段裁剪、凭证过滤及来源确认；本类不证明已执行这些检查。
    source_id/version 供回答引用与交付前重新授权，不把文件内容放进 repr 或日志。
    """

    scope: KnowledgeTaskScope
    source_id: str
    version: str
    source_name: str
    content: bytes = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, KnowledgeTaskScope):
            raise _reject()
        self.scope.__post_init__()
        _component(self.source_id)
        _component(self.version)
        name = self.source_name
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 255
            or name != name.strip()
            or any(c in name for c in ("/", "\\", "\x00"))
            or name.startswith(".")
            or name.casefold() == "agents.md"
            or not isinstance(self.content, bytes)
            or not self.content
            or b"\x00" in self.content
            or not isinstance(self.sha256, str)
            or _HASH.fullmatch(self.sha256) is None
            or hashlib.sha256(self.content).hexdigest() != self.sha256
        ):
            raise _reject()
        try:
            self.content.decode("utf-8", errors="strict")
        except UnicodeError:
            raise _reject() from None


@dataclass(frozen=True)
class KnowledgeDocumentReference:
    """任务内程序文件名与原始资料版本的对应关系，不包含资料正文。"""

    filename: str
    source_id: str
    version: str
    sha256: str


@dataclass(frozen=True)
class _OwnedFile:
    reference: KnowledgeDocumentReference
    identity: tuple[int, int]
    size: int


def _identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _private_directory(fd: int, mode: int) -> None:
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise _reject()


def _open_absolute(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise _reject()
    fd = os.open("/", _DIRECTORY_FLAGS)
    try:
        for name in path.parts[1:]:
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _child_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        _private_directory(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


class PreparedKnowledgeWorkspace:
    """仅在 prepare 上下文存活期间可用的单一工作目录句柄。

    后续受信 wrapper 必须调用 verified_path()，只挂载返回目录，并保持 CLI 无网络工具、
    无共享会话/配置及最小文件系统授权。目录权限不能隔离恶意同 UID 或 root 进程；
    本对象不提供操作系统沙箱，也不能抵抗取得宿主进程权限的攻击者。
    """

    def __init__(
        self, scope: KnowledgeTaskScope, path: Path, parent_fd: int, directory_fd: int
    ) -> None:
        self.scope = scope
        self._path = path
        self._parent_fd = parent_fd
        self._directory_fd = directory_fd
        self._identity = _identity(os.fstat(directory_fd))
        self._files: list[_OwnedFile] = []
        self._active = True

    @property
    def documents(self) -> tuple[KnowledgeDocumentReference, ...]:
        return tuple(value.reference for value in self._files)

    def verified_path(self) -> Path:
        """每次交给执行器前重新验证目录、文件链接数、模式和完整性。"""
        if not self._active:
            raise _reject()
        fd = -1
        try:
            fd = _open_absolute(self._path)
            if _identity(os.fstat(fd)) != self._identity:
                raise _reject()
            _private_directory(fd, 0o500)
            expected = {value.reference.filename for value in self._files}
            if set(os.listdir(fd)) != expected:
                raise _reject()
            for value in self._files:
                file_fd = os.open(
                    value.reference.filename,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
                try:
                    info = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or info.st_uid != os.geteuid()
                        or stat.S_IMODE(info.st_mode) != 0o400
                        or _identity(info) != value.identity
                        or info.st_size != value.size
                    ):
                        raise _reject()
                    content = bytearray()
                    while len(content) <= value.size:
                        chunk = os.read(
                            file_fd, min(65536, value.size + 1 - len(content))
                        )
                        if not chunk:
                            break
                        content.extend(chunk)
                    after = os.fstat(file_fd)
                    if (
                        _identity(after) != value.identity
                        or after.st_nlink != 1
                        or after.st_size != value.size
                        or after.st_mtime_ns != info.st_mtime_ns
                        or after.st_ctime_ns != info.st_ctime_ns
                        or len(content) != value.size
                        or hashlib.sha256(content).hexdigest() != value.reference.sha256
                    ):
                        raise _reject()
                finally:
                    os.close(file_fd)
            return self._path
        except OSError:
            raise _reject() from None
        finally:
            if fd != -1:
                os.close(fd)

    def _cleanup(self) -> None:
        """仅删除本次 inode；替换、额外文件或路径变化时失败而不递归删除。"""
        self._active = False
        try:
            current = os.stat(
                self.scope.run_id, dir_fd=self._parent_fd, follow_symlinks=False
            )
            if _identity(current) != self._identity or not stat.S_ISDIR(
                current.st_mode
            ):
                raise _reject()
            os.fchmod(self._directory_fd, 0o700)
            clean = True
            for value in self._files:
                name = value.reference.filename
                try:
                    current = os.stat(
                        name, dir_fd=self._directory_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    continue
                if (
                    not stat.S_ISREG(current.st_mode)
                    or _identity(current) != value.identity
                ):
                    clean = False
                    continue
                os.unlink(name, dir_fd=self._directory_fd)
            if not clean or os.listdir(self._directory_fd):
                raise _reject()
            os.rmdir(self.scope.run_id, dir_fd=self._parent_fd)
        except OSError:
            raise _reject() from None
        finally:
            os.close(self._directory_fd)
            os.close(self._parent_fd)


class TenantKnowledgeWorkspace:
    """有界同步文件操作避免线程取消后继续写入；异步上下文退出不包含 await。

    root 必须由部署者预建为当前受信服务用户的 0700 私有目录；所有路径分量禁止链接。
    最大值是单次内存/磁盘边界，不替代企业额度。资料仍是不受信文本，不作为 Agent 规则。
    """

    def __init__(
        self,
        root: Path,
        *,
        maximum_documents: int = 32,
        maximum_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._root = Path(root)
        for value in (maximum_documents, maximum_bytes):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise _reject()
        self._maximum_documents, self._maximum_bytes = maximum_documents, maximum_bytes

    @asynccontextmanager
    async def prepare(
        self,
        scope: KnowledgeTaskScope,
        documents: tuple[AuthorizedKnowledgeDocument, ...],
    ) -> AsyncIterator[PreparedKnowledgeWorkspace]:
        """创建当前任务快照，重复 live run 拒绝；正常、异常及取消退出都清理自己的文件。"""
        if not isinstance(scope, KnowledgeTaskScope) or not isinstance(
            documents, tuple
        ):
            raise _reject()
        scope.__post_init__()
        if not documents or len(documents) > self._maximum_documents:
            raise _reject()
        total = 0
        sources: set[tuple[str, str]] = set()
        for document in documents:
            if not isinstance(document, AuthorizedKnowledgeDocument):
                raise _reject()
            document.__post_init__()
            if (
                document.scope != scope
                or (document.source_id, document.version) in sources
            ):
                raise _reject()
            sources.add((document.source_id, document.version))
            total += len(document.content)
        if total > self._maximum_bytes:
            raise _reject()
        handle = None
        root_fd = tenant_fd = parent_fd = directory_fd = -1
        primary: BaseException | None = None
        delivering = False
        created_identity: tuple[int, int] | None = None
        try:
            root_fd = _open_absolute(self._root)
            _private_directory(root_fd, 0o700)
            tenant_fd = _child_directory(root_fd, scope.tenant_id)
            parent_fd = _child_directory(tenant_fd, scope.employee_id)
            os.mkdir(scope.run_id, mode=0o700, dir_fd=parent_fd)
            created_identity = _identity(
                os.stat(scope.run_id, dir_fd=parent_fd, follow_symlinks=False)
            )
            directory_fd = os.open(scope.run_id, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            path = self._root / scope.tenant_id / scope.employee_id / scope.run_id
            handle = PreparedKnowledgeWorkspace(scope, path, parent_fd, directory_fd)
            parent_fd = directory_fd = -1
            for index, document in enumerate(documents, 1):
                name = f"document-{index:04d}.md"
                fd = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=handle._directory_fd,
                )
                try:
                    reference = KnowledgeDocumentReference(
                        name, document.source_id, document.version, document.sha256
                    )
                    handle._files.append(
                        _OwnedFile(
                            reference, _identity(os.fstat(fd)), len(document.content)
                        )
                    )
                    remaining = memoryview(document.content)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise _reject()
                        remaining = remaining[written:]
                    os.fchmod(fd, 0o400)
                finally:
                    os.close(fd)
            os.fchmod(handle._directory_fd, 0o500)
            handle.verified_path()
            delivering = True
            yield handle
        except OSError as error:
            if delivering:
                primary = error
                raise
            primary = _reject()
            raise primary from None
        except BaseException as error:
            primary = error
            raise
        finally:
            if handle is None and created_identity is not None:
                try:
                    current = os.stat(
                        scope.run_id, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if _identity(current) != created_identity or not stat.S_ISDIR(
                        current.st_mode
                    ):
                        raise _reject()
                    os.rmdir(scope.run_id, dir_fd=parent_fd)
                except (OSError, WorkspaceBoundaryError):
                    if primary is not None:
                        primary.add_note("knowledge_workspace_cleanup_incomplete")
            for fd in (directory_fd, parent_fd, tenant_fd, root_fd):
                if fd != -1:
                    os.close(fd)
            if handle is not None:
                try:
                    handle._cleanup()
                except WorkspaceBoundaryError:
                    if primary is None:
                        raise
                    primary.add_note("knowledge_workspace_cleanup_incomplete")
