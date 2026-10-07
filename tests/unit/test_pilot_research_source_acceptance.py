"""本机真实来源入口必须显式授权，且绝不回显私有密钥。"""

from __future__ import annotations

from scripts import pilot_research_source_acceptance as entry


def test_default_does_not_read_private_files(monkeypatch, capsys) -> None:
    def fail_read(*args: object) -> object:
        raise AssertionError("未 opt-in 不得读取 profile")

    monkeypatch.setattr(entry.PilotConfig, "read", fail_read)
    monkeypatch.setattr(entry, "private_read", fail_read)
    result = entry.main(["--profile", "/private/profile"])
    assert result == 0
    assert '"reason":"explicit_opt_in_required"' in capsys.readouterr().out


def test_exclusive_account_required_before_secret_read(monkeypatch, capsys) -> None:
    def fail_read(*args: object) -> object:
        raise AssertionError("未确认专用账户不得读取 profile")

    monkeypatch.setattr(entry.PilotConfig, "read", fail_read)
    monkeypatch.setattr(entry, "private_read", fail_read)
    result = entry.main(
        ["--profile", "/private/profile", "--live", "--budget-confirmed"]
    )
    assert result == 0
    assert '"reason":"exclusive_account_not_confirmed"' in capsys.readouterr().out


def test_invalid_ids_do_not_read_private_files(monkeypatch, capsys) -> None:
    def fail_read(*args: object) -> object:
        raise AssertionError("无效输入不得读取 profile")

    monkeypatch.setattr(entry.PilotConfig, "read", fail_read)
    monkeypatch.setattr(entry, "private_read", fail_read)
    result = entry.main(
        [
            "--profile", "/private/profile", "--live", "--budget-confirmed",
            "--exclusive-account-confirmed", "--proposal-id", "bad-id",
            "--actor-id", "bad-id",
        ]
    )
    assert result == 2
    assert '"reason":"configuration_or_input_invalid"' in capsys.readouterr().out


def test_in_memory_key_is_only_passed_to_existing_gateway_entry(monkeypatch, capsys) -> None:
    fake_key = "tvly-dev-" + "x" * 50
    captured: dict[str, object] = {}

    class FakeConfig:
        def runtime_environment(self) -> dict[str, str]:
            return {"DATABASE_URL": "private-test-dsn"}

        def resolve(self, reference: str) -> str:
            return "fake-" + reference

    monkeypatch.setattr(entry.PilotConfig, "read", lambda path: FakeConfig())
    monkeypatch.setattr(entry, "private_read", lambda path: fake_key.encode())

    def fake_accept(*, environ: dict[str, str], argv: list[str]) -> int:
        captured["environment"] = environ
        captured["arguments"] = argv
        return 0

    monkeypatch.setattr(entry, "accept_sources", fake_accept)
    result = entry.main(
        [
            "--profile", "/private/profile", "--live", "--budget-confirmed",
            "--exclusive-account-confirmed", "--proposal-id", "proposal_1",
            "--actor-id", "actor_1",
        ]
    )
    assert result == 0
    assert captured["arguments"] == [
        "--live", "--budget-confirmed", "--proposal-id", "proposal_1",
        "--actor-id", "actor_1",
    ]
    assert captured["environment"]["PILOT_TAVILY_API_KEY"] == fake_key  # type: ignore[index]
    assert captured["environment"]["TRADEOS_TAVILY_EXCLUSIVE_ACCOUNT_CONFIRMED"] == "true"  # type: ignore[index]
    assert fake_key not in capsys.readouterr().out
