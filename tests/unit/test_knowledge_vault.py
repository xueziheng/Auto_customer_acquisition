"""企业独立只增 Markdown 投影，不借目录名代替上游业务授权。"""

import hashlib
import os

import pytest

from connectors.obsidian.vault import KnowledgeVaultFailure, TenantMarkdownVault


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir(mode=0o700)
    return root, TenantMarkdownVault(root)


async def publish(writer, **patch):
    return await writer.publish(
        **{
            "tenant_id": "tenant_A",
            "document_id": "doc_1",
            "version": 1,
            "status": "awaiting_confirmation",
            "source_text": "Original synthetic source.",
            "markdown": "# 待确认\n\n合成产品资料。",
            **patch,
        }
    )


async def test_tenants_have_disjoint_immutable_sources_and_versions(vault):
    root, writer = vault
    a = await publish(writer)
    b = await publish(
        writer, tenant_id="tenant_B", source_text="B source", markdown="# B"
    )
    assert a.relative_path != b.relative_path
    assert (root / a.relative_path).read_text() == "# 待确认\n\n合成产品资料。"
    assert (root / b.relative_path).read_text() == "# B"
    assert (
        (root / "tenant_A/Sources/doc_1.md").read_text()
        == "# 原始资料（纯文本）\n\n```text\nOriginal synthetic source.\n```\n"
    )
    assert a.sha256 == hashlib.sha256((root / a.relative_path).read_bytes()).hexdigest()
    assert (root / a.relative_path).stat().st_mode & 0o777 == 0o400
    assert (root / "tenant_A").stat().st_mode & 0o777 == 0o700


async def test_identical_retry_is_idempotent_but_changed_content_cannot_overwrite(
    vault,
):
    root, writer = vault
    first = await publish(writer)
    inode = (root / first.relative_path).stat().st_ino
    assert await publish(writer) == first
    assert (root / first.relative_path).stat().st_ino == inode
    with pytest.raises(KnowledgeVaultFailure, match="immutable_conflict"):
        await publish(writer, markdown="# Changed")
    with pytest.raises(KnowledgeVaultFailure, match="immutable_conflict"):
        await publish(writer, source_text="Changed source", version=2)


async def test_new_confirmation_revision_keeps_old_snapshot(vault):
    root, writer = vault
    old = await publish(writer)
    new = await publish(writer, version=2, status="confirmed", markdown="# 已确认")
    assert old.relative_path != new.relative_path
    assert (root / old.relative_path).exists()
    assert (root / new.relative_path).read_text() == "# 已确认"
    assert not list(root.rglob(".pending-*"))


@pytest.mark.parametrize(
    "patch",
    [
        {"tenant_id": "../tenant_B"},
        {"document_id": "a/b"},
        {"tenant_id": ".hidden"},
        {"document_id": "a\\b"},
        {"version": True},
        {"version": 0},
        {"status": "published"},
    ],
)
async def test_untrusted_components_cannot_choose_paths(vault, patch):
    root, writer = vault
    with pytest.raises(KnowledgeVaultFailure):
        await publish(writer, **patch)
    assert not list(root.iterdir())


async def test_symlink_tenant_or_source_is_rejected_without_touching_target(
    vault, tmp_path
):
    root, writer = vault
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (root / "tenant_A").symlink_to(outside, target_is_directory=True)
    with pytest.raises(KnowledgeVaultFailure):
        await publish(writer)
    assert not list(outside.iterdir())
    (root / "tenant_A").unlink()
    await publish(writer)
    source = root / "tenant_A/Sources/doc_1.md"
    source.unlink()
    outside_file = outside / "source"
    outside_file.write_text("outside")
    source.symlink_to(outside_file)
    with pytest.raises(KnowledgeVaultFailure):
        await publish(writer)
    assert outside_file.read_text() == "outside"


async def test_hardlink_alias_of_immutable_source_is_rejected(vault, tmp_path):
    root, writer = vault
    await publish(writer)
    source = root / "tenant_A/Sources/doc_1.md"
    os.link(source, tmp_path / "alias")
    with pytest.raises(KnowledgeVaultFailure):
        await publish(writer)


async def test_world_readable_root_and_symlink_ancestor_are_rejected(vault, tmp_path):
    root, writer = vault
    root.chmod(0o755)
    with pytest.raises(KnowledgeVaultFailure):
        await publish(writer)
    root.chmod(0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(KnowledgeVaultFailure):
        await publish(TenantMarkdownVault(alias))


async def test_limits_fail_before_any_write(vault):
    root, _ = vault
    writer = TenantMarkdownVault(root, max_source_bytes=8)
    with pytest.raises(KnowledgeVaultFailure, match="vault_size_limit"):
        await publish(writer)
    assert not list(root.iterdir())


async def test_source_markdown_cannot_close_literal_fence(vault):
    root, writer = vault
    raw = "\u0060\u0060\u0060\u0060\u0060\n![remote](https://example.invalid/image)\n<iframe src=x></iframe>"
    await publish(writer, source_text=raw)
    stored = (root / "tenant_A/Sources/doc_1.md").read_text()
    assert "\u0060\u0060\u0060\u0060\u0060\u0060text\n" in stored
    assert stored.endswith("\n\u0060\u0060\u0060\u0060\u0060\u0060\n")
    assert raw in stored
