"""只读后台按持久绑定选择邮箱，不接受命令行覆盖私有身份。"""

import argparse
from pathlib import Path

import pytest

from apps.email_feedback_worker import mailbox
from infra.pilot.mailbox_config import MailboxConfig
from shared.schemas.mailbox import MailboxFailure


def _args(path: Path, selected: str, **overrides):
    values = {
        "profile": None, "mailbox_profile": path, "binding_id": selected,
        "employee_id": None, "email": None, "credentials_file": None,
    }
    return argparse.Namespace(**(values | overrides))


def test_persistent_worker_selects_exact_binding_without_business_policy(tmp_path):
    path = tmp_path / "profile" / "config.json"
    MailboxConfig.create(path)
    cfg = MailboxConfig.bind(path, employee_id="emp_01ARZ3NDEKTSV4RRFFQ69G5FAV", email="one@example.com",
                             credentials_file=Path("/private/first.json"))
    cfg = MailboxConfig.bind(path, employee_id="emp_01ARZ3NDEKTSV4RRFFQ69G5FAV", email="two@example.com",
                             credentials_file=Path("/private/second.json"))
    load = getattr(mailbox, "load_sync_configuration", None)
    assert callable(load), "持久邮箱入口尚未实现"
    runtime = load(_args(path, cfg.bindings[1].binding_id))
    assert runtime.email == "two@example.com"
    assert runtime.employee_id == "emp_01ARZ3NDEKTSV4RRFFQ69G5FAV"
    assert runtime.credentials_file == Path("/private/second.json")
    assert runtime.tenant_id == cfg.tenant_id
    assert cfg.db_password.get_secret_value() not in repr(runtime)
    assert cfg.fingerprint_key.get_secret_value() not in repr(runtime)


@pytest.mark.parametrize("overrides", [
    {"employee_id": "emp_other"}, {"email": "other@example.com"},
    {"credentials_file": Path("/private/other.json")}, {"profile": Path("/other.json")},
    {"binding_id": "mbb_missing"},
])
def test_persistent_worker_rejects_ambiguous_or_foreign_binding(tmp_path, overrides):
    path = tmp_path / "profile" / "config.json"
    MailboxConfig.create(path)
    cfg = MailboxConfig.bind(path, employee_id="emp_01ARZ3NDEKTSV4RRFFQ69G5FAV", email="one@example.com",
                             credentials_file=Path("/private/first.json"))
    load = getattr(mailbox, "load_sync_configuration", None)
    assert callable(load), "持久邮箱入口尚未实现"
    with pytest.raises(MailboxFailure):
        load(_args(path, cfg.bindings[0].binding_id, **overrides))
