"""企业专属、仅追加的 Markdown 投影；业务事实与权限仍归受信上游。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_STATUSES = frozenset({"awaiting_confirmation", "confirmed"})
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class KnowledgeVaultFailure(ValueError):
    """只暴露固定分类，不回显企业路径、正文或底层异常。"""

    def __init__(self, reason: str = "vault_boundary_rejected") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PublishedKnowledgeSnapshot:
    relative_path: str
    sha256: str
    source_sha256: str


def _component(value: str) -> None:
    if not isinstance(value, str) or _COMPONENT.fullmatch(value) is None:
        raise KnowledgeVaultFailure()


def _private(fd: int) -> tuple[int, int]:
    info = os.fstat(fd)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise KnowledgeVaultFailure()
    return info.st_dev, info.st_ino


def _root(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise KnowledgeVaultFailure()
    fd = os.open("/", _DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = child
        _private(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _directory(parent: int, name: str) -> int:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    fd = os.open(name, _DIRECTORY, dir_fd=parent)
    try:
        _private(fd)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _verify(parent: int, name: str, expected: bytes) -> str:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o400
            or before.st_nlink != 1
            or before.st_size != len(expected)
        ):
            raise KnowledgeVaultFailure("immutable_conflict")
        content = bytearray()
        while len(content) <= len(expected):
            part = os.read(fd, min(65536, len(expected) + 1 - len(content)))
            if not part:
                break
            content.extend(part)
        after = os.fstat(fd)
        if (
            before.st_dev != after.st_dev
            or before.st_ino != after.st_ino
            or before.st_ctime_ns != after.st_ctime_ns
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_nlink != after.st_nlink
            or bytes(content) != expected
        ):
            raise KnowledgeVaultFailure("immutable_conflict")
        return hashlib.sha256(content).hexdigest()
    finally:
        os.close(fd)


def _append(parent: int, name: str, content: bytes) -> str:
    """先写完整私有 inode，再原子创建最终名字；从不替换现有版本。"""
    try:
        return _verify(parent, name, content)
    except FileNotFoundError:
        pass
    temporary = ".pending-" + uuid.uuid4().hex
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=parent,
    )
    identity = os.fstat(fd).st_ino
    linked = False
    try:
        remaining = memoryview(content)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise KnowledgeVaultFailure()
            remaining = remaining[written:]
        os.fchmod(fd, 0o400)
        os.fsync(fd)
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
            linked = True
        except FileExistsError:
            pass
    finally:
        os.close(fd)
        current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
        if current.st_ino != identity or not stat.S_ISREG(current.st_mode):
            raise KnowledgeVaultFailure()
        os.unlink(temporary, dir_fd=parent)
    if linked:
        os.fsync(parent)
    return _verify(parent, name, content)


def _literal_source(text: str) -> str:
    """以足够长的 text fence 保存原文，不激活上传 Markdown 的链接或嵌入。"""
    longest = max((len(value) for value in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return "# 原始资料（纯文本）\n\n" + fence + "text\n" + text + "\n" + fence + "\n"


class TenantMarkdownVault:
    """固定企业/文档/版本路径；不扫描其他资料、不生成业务结论、不提供模型写文件工具。"""

    def __init__(
        self,
        root: Path,
        *,
        max_source_bytes: int = 1024 * 1024,
        max_markdown_bytes: int = 1024 * 1024,
    ) -> None:
        for value in (max_source_bytes, max_markdown_bytes):
            if type(value) is not int or value <= 0 or value > 4 * 1024 * 1024:
                raise KnowledgeVaultFailure()
        self._root = root
        self._max_source_bytes = max_source_bytes
        self._max_markdown_bytes = max_markdown_bytes

    async def publish(
        self,
        *,
        tenant_id: str,
        document_id: str,
        version: int,
        status: str,
        source_text: str,
        markdown: str,
    ) -> PublishedKnowledgeSnapshot:
        """只发布该版本；同版本同内容幂等，异内容、软硬链接与共享权限拒绝。

        有界本地写没有 await/后台线程，取消不会遗留继续写文件的工作线程。上游须在调用前后重核
        来源与业务版本；该导出不把待确认提取提升为已发布产品，也不自动改变 canonical 状态。
        """
        _component(tenant_id)
        _component(document_id)
        if (
            type(version) is not int
            or version <= 0
            or not isinstance(status, str)
            or status not in _STATUSES
        ):
            raise KnowledgeVaultFailure()
        if not isinstance(source_text, str) or not isinstance(markdown, str):
            raise KnowledgeVaultFailure()
        if (
            not source_text.strip()
            or not markdown.strip()
            or "\x00" in source_text + markdown
        ):
            raise KnowledgeVaultFailure()
        try:
            source = _literal_source(source_text).encode("utf-8")
            rendered = markdown.encode("utf-8")
        except UnicodeError:
            raise KnowledgeVaultFailure() from None
        if (
            len(source) > self._max_source_bytes
            or len(rendered) > self._max_markdown_bytes
        ):
            raise KnowledgeVaultFailure("vault_size_limit")
        root_fd = tenant_fd = sources_fd = docs_fd = document_fd = -1
        try:
            root_fd = _root(self._root)
            root_identity = _private(root_fd)
            tenant_fd = _directory(root_fd, tenant_id)
            tenant_identity = _private(tenant_fd)
            sources_fd = _directory(tenant_fd, "Sources")
            sources_identity = _private(sources_fd)
            docs_fd = _directory(tenant_fd, "Docs")
            docs_identity = _private(docs_fd)
            document_fd = _directory(docs_fd, document_id)
            document_identity = _private(document_fd)
            source_hash = _append(sources_fd, document_id + ".md", source)
            name = f"v{version}-{status}.md"
            content_hash = _append(document_fd, name, rendered)
            # 返回给受信worker之前，确认路径仍指向本次持有的目录。
            again = _root(self._root)
            try:
                if _private(again) != root_identity:
                    raise KnowledgeVaultFailure()
                for parent, child_name, identity in (
                    (root_fd, tenant_id, tenant_identity),
                    (tenant_fd, "Sources", sources_identity),
                    (tenant_fd, "Docs", docs_identity),
                    (docs_fd, document_id, document_identity),
                ):
                    info = os.stat(child_name, dir_fd=parent, follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(info.st_mode)
                        or (info.st_dev, info.st_ino) != identity
                    ):
                        raise KnowledgeVaultFailure()
            finally:
                os.close(again)
            return PublishedKnowledgeSnapshot(
                relative_path=f"{tenant_id}/Docs/{document_id}/{name}",
                sha256=content_hash,
                source_sha256=source_hash,
            )
        except KnowledgeVaultFailure:
            raise
        except (OSError, UnicodeError):
            raise KnowledgeVaultFailure() from None
        finally:
            for fd in (document_fd, docs_fd, sources_fd, tenant_fd, root_fd):
                if fd != -1:
                    os.close(fd)
