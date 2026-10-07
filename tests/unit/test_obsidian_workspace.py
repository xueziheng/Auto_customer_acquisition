"""企业资料任务快照：真实文件系统合成样本，不启动模型。"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace

import pytest

from connectors.obsidian.workspace import (
    AuthorizedKnowledgeDocument,
    KnowledgeTaskScope,
    TenantKnowledgeWorkspace,
    WorkspaceBoundaryError,
)


def scope(tenant="tenant_a", employee="employee_a", run="run_a"):
    return KnowledgeTaskScope(tenant, employee, run)


def doc(bound=None, content=b"# Synthetic product\nSize: 20 mm\n", name="catalog.md"):
    return AuthorizedKnowledgeDocument(
        scope=bound or scope(),
        source_id="source_a",
        version="v1",
        source_name=name,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


@pytest.fixture
def root(tmp_path):
    value = tmp_path / "workspaces"
    value.mkdir(mode=0o700)
    return value


async def test_snapshot_is_read_only_and_uses_program_names(root):
    factory = TenantKnowledgeWorkspace(root)
    async with factory.prepare(scope(), (doc(),)) as handle:
        path = handle.verified_path()
        assert path == root / "tenant_a" / "employee_a" / "run_a"
        assert sorted(p.name for p in path.iterdir()) == ["document-0001.md"]
        assert (path / "document-0001.md").read_bytes() == doc().content
        assert path.stat().st_mode & 0o777 == 0o500
        assert (path / "document-0001.md").stat().st_mode & 0o777 == 0o400
        assert handle.documents[0].source_id == "source_a"
        assert handle.documents[0].sha256 == doc().sha256
    assert not path.exists()
    with pytest.raises(WorkspaceBoundaryError):
        handle.verified_path()


async def test_same_run_and_name_in_two_tenants_never_reuse_bytes(root):
    factory = TenantKnowledgeWorkspace(root)
    other = scope("tenant_b")
    async with (
        factory.prepare(scope(), (doc(),)) as left,
        factory.prepare(other, (doc(other, b"Tenant B"),)) as right,
    ):
        assert left.verified_path() != right.verified_path()
        assert (left.verified_path() / "document-0001.md").read_bytes() == doc().content
        assert (right.verified_path() / "document-0001.md").read_bytes() == b"Tenant B"


@pytest.mark.parametrize(
    "bound", [scope("tenant_b"), scope(employee="employee_b"), scope(run="run_b")]
)
async def test_mixed_scope_rejected_before_creating_task(root, bound):
    with pytest.raises(WorkspaceBoundaryError):
        async with TenantKnowledgeWorkspace(root).prepare(scope(), (doc(bound),)):
            pytest.fail("混合身份不得生成资料快照")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("tenant_id", "../other"),
        ("tenant_id", "/tmp"),
        ("employee_id", "a/b"),
        ("run_id", "."),
        ("run_id", "a\\b"),
        ("tenant_id", ""),
        ("run_id", "x" * 129),
    ],
)
def test_scope_rejects_path_injection(field, value):
    with pytest.raises(WorkspaceBoundaryError):
        replace(scope(), **{field: value})


@pytest.mark.parametrize(
    "name",
    [
        "AGENTS.md",
        "agents.MD",
        ".codex",
        ".codex/config.toml",
        "../a.md",
        "/tmp/a.md",
        "a\\b.md",
        "a/b.md",
        "",
        "catalog\x00.md",
    ],
)
def test_document_rejects_paths_and_instruction_filenames(name):
    with pytest.raises(WorkspaceBoundaryError):
        doc(name=name)


async def test_duplicate_live_task_does_not_remove_original(root):
    factory = TenantKnowledgeWorkspace(root)
    async with factory.prepare(scope(), (doc(),)) as first:
        with pytest.raises(WorkspaceBoundaryError):
            async with factory.prepare(scope(), (doc(),)):
                pytest.fail("相同任务不能覆盖")
        assert (
            first.verified_path() / "document-0001.md"
        ).read_bytes() == doc().content


def test_document_integrity_and_non_markdown_bytes_rejected():
    with pytest.raises(WorkspaceBoundaryError):
        replace(doc(), sha256="0" * 64)
    with pytest.raises(WorkspaceBoundaryError):
        doc(content=b"\xff")
    with pytest.raises(WorkspaceBoundaryError):
        doc(content=b"abc\x00def")


async def test_size_and_count_limits_fail_before_filesystem_io(root):
    factory = TenantKnowledgeWorkspace(root, maximum_documents=1, maximum_bytes=4)
    for documents in [(doc(),), (doc(content=b"a"), doc(content=b"b"))]:
        with pytest.raises(WorkspaceBoundaryError):
            async with factory.prepare(scope(), documents):
                pytest.fail("超过上限必须失败")
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("level", ["tenant", "employee", "run"])
async def test_existing_symlink_in_scope_never_followed(root, tmp_path, level):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    sentinel = outside / "untouched"
    sentinel.write_text("safe")
    parent = root
    for name in ["tenant_a", "employee_a", "run_a"]:
        if (
            name
            == {"tenant": "tenant_a", "employee": "employee_a", "run": "run_a"}[level]
        ):
            (parent / name).symlink_to(outside, target_is_directory=True)
            break
        parent = parent / name
        parent.mkdir(mode=0o700)
    with pytest.raises(WorkspaceBoundaryError):
        async with TenantKnowledgeWorkspace(root).prepare(scope(), (doc(),)):
            pytest.fail("不允许软链接")
    assert sentinel.read_text() == "safe"
    assert sorted(p.name for p in outside.iterdir()) == ["untouched"]


async def test_symlink_root_and_public_root_rejected(root, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(root, target_is_directory=True)
    for candidate in [link, root]:
        if candidate == root:
            root.chmod(0o755)
        with pytest.raises(WorkspaceBoundaryError):
            async with TenantKnowledgeWorkspace(candidate).prepare(scope(), (doc(),)):
                pytest.fail("根目录必须私有且不可为链接")


async def test_symlink_ancestor_rejected(root, tmp_path):
    link = tmp_path / "ancestor"
    link.symlink_to(root.parent, target_is_directory=True)
    with pytest.raises(WorkspaceBoundaryError):
        async with TenantKnowledgeWorkspace(link / root.name).prepare(
            scope(), (doc(),)
        ):
            pytest.fail("祖先链接不能绕过约束")


@pytest.mark.parametrize(
    "attack", ["symlink", "hardlink", "changed_bytes", "extra_file"]
)
async def test_handle_revalidates_before_wrapper_use(root, tmp_path, attack):
    factory = TenantKnowledgeWorkspace(root)
    with pytest.raises(WorkspaceBoundaryError):
        async with factory.prepare(scope(), (doc(),)) as handle:
            path = handle.verified_path()
            leaf = path / "document-0001.md"
            if attack == "hardlink":
                os.link(leaf, tmp_path / "outside_link")
            elif attack == "changed_bytes":
                leaf.chmod(0o600)
                leaf.write_bytes(b"changed")
                leaf.chmod(0o400)
            else:
                path.chmod(0o700)
                if attack == "symlink":
                    leaf.unlink()
                    leaf.symlink_to(tmp_path / "missing")
                else:
                    (path / "AGENTS.md").write_text("untrusted")
                path.chmod(0o500)
            handle.verified_path()


async def test_exception_removes_only_owned_task(root):
    factory = TenantKnowledgeWorkspace(root)
    other = scope(run="run_b")
    async with factory.prepare(other, (doc(other),)) as unaffected:
        with pytest.raises(RuntimeError, match="synthetic"):
            async with factory.prepare(scope(), (doc(),)) as handle:
                path = handle.verified_path()
                raise RuntimeError("synthetic")
        assert not path.exists()
        assert unaffected.verified_path().exists()


async def test_cancellation_cleans_before_propagating(root):
    ready = asyncio.Event()
    holder = {}

    async def worker():
        async with TenantKnowledgeWorkspace(root).prepare(scope(), (doc(),)) as handle:
            holder["path"] = handle.verified_path()
            ready.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(worker())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not holder["path"].exists()


async def test_replaced_run_directory_never_deleted(root):
    factory = TenantKnowledgeWorkspace(root)
    with pytest.raises(WorkspaceBoundaryError):
        async with factory.prepare(scope(), (doc(),)) as handle:
            path = handle.verified_path()
            moved = path.with_name("moved")
            path.rename(moved)
            path.mkdir(mode=0o700)
            marker = path / "foreign"
            marker.write_text("preserve")
    assert marker.read_text() == "preserve"
    assert moved.exists()


async def test_creation_failure_cleans_partial_files(root, monkeypatch):
    original = os.write

    def broken(fd, value):
        original(fd, value[:1])
        raise OSError("synthetic failure")

    monkeypatch.setattr(os, "write", broken)
    with pytest.raises(WorkspaceBoundaryError):
        async with TenantKnowledgeWorkspace(root).prepare(scope(), (doc(),)):
            pytest.fail("写入失败不能交付")
    assert not (root / "tenant_a" / "employee_a" / "run_a").exists()


async def test_caller_oserror_is_preserved_after_cleanup(root):
    failure = OSError("caller_failure")
    with pytest.raises(OSError) as raised:
        async with TenantKnowledgeWorkspace(root).prepare(scope(), (doc(),)) as handle:
            path = handle.verified_path()
            raise failure
    assert raised.value is failure
    assert not path.exists()


async def test_open_new_directory_failure_removes_only_created_empty_task(
    root, monkeypatch
):
    original = os.open

    def broken(path, flags, *args, **kwargs):
        if path == "run_a":
            raise OSError("synthetic directory open failure")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", broken)
    with pytest.raises(WorkspaceBoundaryError):
        async with TenantKnowledgeWorkspace(root).prepare(scope(), (doc(),)):
            pytest.fail("目录未打开不能交付")
    assert not (root / "tenant_a" / "employee_a" / "run_a").exists()


async def test_source_path_is_not_a_supported_document_input(root):
    with pytest.raises(WorkspaceBoundaryError):
        replace(doc(), content=root / "tenant_b" / "catalog.md")


async def test_scope_and_content_are_revalidated_before_preparation(root):
    item = doc()
    object.__setattr__(item, "content", b"changed")
    with pytest.raises(WorkspaceBoundaryError):
        async with TenantKnowledgeWorkspace(root).prepare(scope(), (item,)):
            pytest.fail("冻结对象被绕过后仍须验证")
    assert list(root.iterdir()) == []
