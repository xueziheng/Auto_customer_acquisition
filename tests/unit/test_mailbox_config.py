"""专用邮箱配置不依赖业务政策，不读取 OAuth，并保留员工归属。"""

import os
from pathlib import Path

import pytest

from infra.pilot.config import PilotError
from shared.schemas.identifiers import new_id


def test_private_config_roundtrip_has_no_business_seed(tmp_path: Path) -> None:
    from infra.pilot.mailbox_config import MailboxConfig

    path = tmp_path / "mailbox/config.json"
    created = MailboxConfig.create(path)
    current = MailboxConfig.read(path)
    assert current == created
    assert current.bindings == () and current.storage is None
    assert current.api_port != current.db_port
    assert current.origin == f"http://127.0.0.1:{current.api_port}"
    assert "127.0.0.1" in current.database_url.get_secret_value()
    assert current.db_password.get_secret_value() not in repr(current)
    assert current.fingerprint_key.get_secret_value() not in repr(current)
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert not any(
        x in MailboxConfig.model_fields for x in ("policy", "model", "minio")
    )
    with pytest.raises(PilotError, match="profile_exists"):
        MailboxConfig.create(path)
    assert MailboxConfig.read(path) == current


@pytest.mark.parametrize("mode", [0o644, 0o666])
def test_private_config_rejects_readable_credentials(tmp_path: Path, mode: int) -> None:
    from infra.pilot.mailbox_config import MailboxConfig

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    path.chmod(mode)
    with pytest.raises(PilotError, match="configuration_invalid"):
        MailboxConfig.read(path)


def test_bindings_allow_multiple_mailboxes_without_reading_oauth(
    tmp_path: Path,
) -> None:
    from infra.pilot.mailbox_config import MailboxConfig

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    employee = str(new_id("emp"))
    credentials = tmp_path / "not-yet-authorized.json"
    first = MailboxConfig.bind(
        path,
        employee_id=employee,
        email="owner@example.test",
        credentials_file=credentials,
    )
    again = MailboxConfig.bind(
        path,
        employee_id=employee,
        email="owner@example.test",
        credentials_file=credentials,
    )
    assert again == first
    second = MailboxConfig.bind(
        path,
        employee_id=employee,
        email="second@example.test",
        credentials_file=tmp_path / "second.json",
    )
    assert len(second.bindings) == 2
    assert len({b.binding_id for b in second.bindings}) == 2
    assert not credentials.exists()
    assert MailboxConfig.read(path) == second


def test_bindings_cannot_transfer_owner_or_replace_path(tmp_path: Path) -> None:
    from infra.pilot.mailbox_config import MailboxConfig

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    employee = str(new_id("emp"))
    first = MailboxConfig.bind(
        path,
        employee_id=employee,
        email="owner@example.test",
        credentials_file=tmp_path / "one.json",
    )
    for owner, credentials in (
        (str(new_id("emp")), tmp_path / "one.json"),
        (employee, tmp_path / "two.json"),
    ):
        with pytest.raises(PilotError, match="binding_conflict"):
            MailboxConfig.bind(
                path,
                employee_id=owner,
                email="OWNER@example.test",
                credentials_file=credentials,
            )
    assert MailboxConfig.read(path) == first


def test_binding_rejects_relative_path(tmp_path: Path) -> None:
    from infra.pilot.mailbox_config import MailboxConfig

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    with pytest.raises(PilotError, match="binding_invalid"):
        MailboxConfig.bind(
            path,
            employee_id=str(new_id("emp")),
            email="owner@example.test",
            credentials_file=Path("relative.json"),
        )
    assert MailboxConfig.read(path).bindings == ()


def test_config_refuses_links(tmp_path: Path) -> None:
    from infra.pilot.mailbox_config import MailboxConfig

    path = tmp_path / "mailbox/config.json"
    MailboxConfig.create(path)
    original = path.with_name("original.json")
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises(PilotError, match="configuration_invalid"):
        MailboxConfig.read(path)
    path.unlink()
    os.link(original, path)
    with pytest.raises(PilotError, match="configuration_invalid"):
        MailboxConfig.read(path)
