"""未提供明确配置和研究输入时，验收不得访问真实 Provider。"""

import pytest

from scripts.accept_builtin_agent import parse_args


def test_live_requires_explicit_input_and_settings():
    with pytest.raises(SystemExit):
        parse_args(["--live"])


def test_default_is_controlled_without_secrets():
    args = parse_args([])
    assert not args.live and args.settings_file is None


@pytest.mark.parametrize(
    "outcome", ["success", "proposal_changed", "probe_failed", "run_failed"]
)
def test_live_acceptance_confirms_only_exact_approved_fields(monkeypatch, outcome):
    import argparse
    import json
    from types import SimpleNamespace

    import httpx

    from scripts import accept_builtin_agent as module

    model = SimpleNamespace(
        model="test-model",
        configuration_version="test-v1",
        limits=SimpleNamespace(timeout_seconds=1),
    )
    monkeypatch.setattr(module, "load_model_settings", lambda path: model)
    monkeypatch.setattr(
        module,
        "private_read",
        lambda path: json.dumps(
            {
                "base_url": "http://127.0.0.1:8080",
                "username": "boss",
                "messages": ["目标：研究"],
                "expected_parsed_fields": {"objective": "研究"},
            }
        ).encode(),
    )
    monkeypatch.setattr(module.getpass, "getpass", lambda prompt: "CONTROLLED_PASSWORD")
    calls = []
    probed = False

    def transport(request):
        nonlocal probed
        path = request.url.path
        calls.append((request.method, path))
        if path == "/api/auth/login":
            return httpx.Response(200, json={"csrf_token": "controlled"})
        if path == "/api/auth/logout":
            return httpx.Response(204)
        if request.method == "POST":
            assert request.headers["x-csrf-token"] == "controlled"
        if path == "/api/settings/model/probe":
            probed = True
            return httpx.Response(202, json={"turn_id": "probe"})
        if path == "/api/settings/model":
            return httpx.Response(
                200,
                json={
                    "model": "test-model",
                    "configuration_version": "test-v1",
                    "probe_turn_id": "probe" if probed else None,
                    "probe_state": "completed",
                    "status": "configured_unverified"
                    if outcome == "probe_failed"
                    else "verified",
                },
            )
        if path == "/api/agent/sessions":
            return httpx.Response(201, json={"session_id": "session"})
        if path == "/api/agent/sessions/session/turns":
            return httpx.Response(202, json={"turn_id": "turn"})
        if path == "/api/agent/sessions/session/turns/turn":
            return httpx.Response(
                200, json={"state": "proposal_ready", "proposal_id": "proposal"}
            )
        if path == "/api/commands/discovery-proposals/proposal":
            return httpx.Response(
                200,
                json={
                    "execution_mode": "research_only",
                    "can_confirm": True,
                    "parsed_fields": {
                        "objective": "CHANGED"
                        if outcome == "proposal_changed"
                        else "研究"
                    },
                },
            )
        if path == "/api/commands/discovery-proposals/proposal/confirm":
            return httpx.Response(202, json={"run_id": "run"})
        if path == "/api/runs/run":
            return httpx.Response(
                200,
                json={
                    "summary": {
                        "status": "failed" if outcome == "run_failed" else "completed",
                        "research": {"signal_count": 1},
                    }
                },
            )
        raise AssertionError(path)

    real_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(transport)
        ),
    )
    report = {"live_model": "not_run", "live_sources": "not_run"}
    args = argparse.Namespace(
        settings_file="controlled", approved_research_input="controlled"
    )
    if outcome == "success":
        module._live(args, report)
    else:
        with pytest.raises(ValueError):
            module._live(args, report)
    assert calls[-1] == ("POST", "/api/auth/logout")
    confirmed = ("POST", "/api/commands/discovery-proposals/proposal/confirm") in calls
    assert confirmed is (outcome in ("success", "run_failed"))
    assert report["live_model"] == ("failed" if outcome == "probe_failed" else "passed")
    assert (
        report["live_sources"]
        == {
            "success": "passed",
            "proposal_changed": "not_run",
            "probe_failed": "not_run",
            "run_failed": "failed",
        }[outcome]
    )
