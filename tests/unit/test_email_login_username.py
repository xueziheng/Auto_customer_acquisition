"""邮箱登录只做确定性规范化，不合并 Gmail 点号或加号地址。"""

import secrets

import pytest
from pydantic import SecretStr, ValidationError

from apps.api.authentication import LoginRequest
from apps.api.pilot_accounts import AccountCommand
from shared.authentication import AuthenticationInputInvalid


@pytest.mark.parametrize(
    "username, expected",
    [
        ("owner", "owner"),
        ("owner.name-1", "owner.name-1"),
        ("User.Name+Trade@GMAIL.COM", "user.name+trade@gmail.com"),
        (
            "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 61,
            "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 61,
        ),
    ],
)
def test_login_and_bootstrap_share_email_normalization(username, expected):
    request = LoginRequest(
        username=username, password=SecretStr(secrets.token_urlsafe(24))
    )
    command = AccountCommand("create", username, name="合成测试账户", role="viewer")
    assert request.username == expected
    assert command.username == expected


@pytest.mark.parametrize(
    "username",
    [
        "UPPER",
        " user@gmail.com",
        "user@gmail.com ",
        "user@@gmail.com",
        "user@localhost",
        ".user@gmail.com",
        "user..name@gmail.com",
        "user.@gmail.com",
        "user@-gmail.com",
        "user@gmail-.com",
        "user@gm ail.com",
        "user\x00@gmail.com",
        "用户@gmail.com",
        "a" * 65 + "@gmail.com",
        "user@" + "b" * 64 + ".com",
        "a" * 64 + "@" + "b" * 63 + "." + "c" * 63 + "." + "d" * 62,
    ],
)
def test_invalid_email_username_is_rejected_without_echo(username):
    with pytest.raises(ValidationError) as caught:
        LoginRequest(username=username, password=SecretStr(secrets.token_urlsafe(24)))
    assert username not in str(caught.value)
    with pytest.raises(AuthenticationInputInvalid):
        AccountCommand("create", username, name="合成测试账户", role="viewer")


@pytest.mark.parametrize("action", ["reset-password", "enable", "disable"])
def test_account_maintenance_normalizes_email_username(action):
    assert (
        AccountCommand(action, "User.Name+Trade@GMAIL.COM").username
        == "user.name+trade@gmail.com"
    )
