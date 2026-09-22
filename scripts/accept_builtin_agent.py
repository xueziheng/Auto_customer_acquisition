"""内置 Agent 分层验收；真实模式仅通过已运行的认证产品 API。"""

from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from infra.pilot.config import private_read
from infra.standalone.settings import load_model_settings

ROOT = Path(__file__).resolve().parents[1]
CONTROLLED = [
    "tests/unit/test_deepseek_client.py",
    "tests/integration/test_model_gateway.py",
    "tests/integration/test_assistant_reads.py",
    "tests/integration/test_assistant_proposal.py",
    "tests/integration/test_assistant_recovery.py",
    "tests/integration/test_assistant_api.py",
    "tests/integration/test_standalone_model_runtime.py",
    "tests/integration/test_builtin_research.py",
    "tests/evals/test_builtin_agent_evals.py",
]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="内置 Agent 分层验收")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--settings-file", type=Path)
    parser.add_argument("--approved-research-input", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.live and not (args.settings_file and args.approved_research_input):
        parser.error("真实验收需要部署配置及明确的研究输入")
    return args


class ApprovedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    base_url: str
    username: str = Field(min_length=1, max_length=128)
    messages: list[str] = Field(min_length=1, max_length=20)
    expected_parsed_fields: dict[str, str]

    @field_validator("base_url")
    @classmethod
    def trusted_origin(cls, value: str) -> str:
        url = urlsplit(value)
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("需要产品同源入口")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost")
        ):
            raise ValueError("需要 HTTPS 或本机入口")
        return value.rstrip("/")


def _live(args: argparse.Namespace, report: dict[str, object]) -> None:
    settings = load_model_settings(args.settings_file)
    approved = ApprovedInput.model_validate_json(
        private_read(args.approved_research_input)
    )
    report.update(
        configuration_version=settings.configuration_version, model_id=settings.model
    )
    with httpx.Client(
        base_url=approved.base_url, timeout=30, follow_redirects=False, trust_env=False
    ) as client:
        headers = {"Origin": approved.base_url, "X-TradeOS-Request": "1"}
        password = getpass.getpass("产品账号密码（不保存）：")
        try:
            response = client.post(
                "/api/auth/login",
                headers=headers,
                json={"username": approved.username, "password": password},
            )
        finally:
            del password
        if response.status_code != 200:
            raise ValueError("登录失败")
        headers["X-CSRF-Token"] = response.json()["csrf_token"]
        from uuid import uuid4

        def read(path: str) -> object:
            result = client.get("/api" + path)
            if result.status_code != 200:
                raise ValueError("读取失败")
            return result.json()

        def post(path: str, body: dict[str, object]) -> dict:
            result = client.post("/api" + path, headers=headers, json=body)
            if result.status_code not in (200, 201, 202):
                raise ValueError("操作未接纳")
            return result.json()

        def wait_for(read_value, completed):
            deadline = time.monotonic() + max(120, settings.limits.timeout_seconds * 4)
            while time.monotonic() < deadline:
                value = read_value()
                if completed(value):
                    return value
                time.sleep(2)
            raise ValueError("后台任务未在验收窗口完成")

        try:
            current = read("/settings/model")
            if (
                current["model"] != settings.model
                or current["configuration_version"] != settings.configuration_version
            ):
                raise ValueError("运行配置不匹配")
            report["live_model"] = "failed"
            probe = post("/settings/model/probe", {"idempotency_key": str(uuid4())})
            current = wait_for(
                lambda: read("/settings/model"),
                lambda v: (
                    v["probe_turn_id"] == probe["turn_id"]
                    and v["probe_state"] not in ("queued", "running")
                ),
            )
            if current["status"] != "verified":
                raise ValueError("连接未验证")
            report["live_model"] = "passed"
            session = post("/agent/sessions", {})
            path = f"/agent/sessions/{session['session_id']}/turns"
            last = None
            for message in approved.messages:
                turn = post(
                    path,
                    {
                        "text": message,
                        "object_refs": [],
                        "idempotency_key": str(uuid4()),
                    },
                )
                last = wait_for(
                    lambda turn_id=turn["turn_id"]: read(path + "/" + turn_id),
                    lambda v: v["state"] not in ("queued", "running"),
                )
                if last["state"] in ("failed", "blocked", "unknown", "cancelled"):
                    raise ValueError("对话未完成")
            if last is None or not last["proposal_id"]:
                raise ValueError("尚未形成研究提案")
            proposal_path = "/commands/discovery-proposals/" + last["proposal_id"]
            proposal = read(proposal_path)
            if (
                proposal["execution_mode"] != "research_only"
                or not proposal["can_confirm"]
                or proposal["parsed_fields"] != approved.expected_parsed_fields
            ):
                raise ValueError("提案与人工预批准字段不完全相同，未确认")
            report["live_sources"] = "failed"
            receipt = post(proposal_path + "/confirm", {})
            run = wait_for(
                lambda: read("/runs/" + receipt["run_id"]),
                lambda v: (
                    v["summary"]["status"] in ("completed", "failed", "cancelled")
                ),
            )
            if run["summary"]["status"] != "completed":
                raise ValueError("研究未完成")
            research = run["summary"].get("research")
            if not research or research.get("signal_count", 0) < 1:
                raise ValueError("尚无可核验研究信号")
            report["live_sources"] = "passed"
        finally:
            result = client.post("/api/auth/logout", headers=headers)
            if result.status_code != 204:
                raise ValueError("退出未确认")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    report: dict[str, object] = {
        "source_commit": source,
        "configuration_version": None,
        "model_id": None,
        "prompt_version": "assistant-v1",
        "controlled": "not_run",
        "live_model": "not_run",
        "live_sources": "not_run",
        "shared_deployment": "not_run",
    }
    status = 0
    try:
        if args.live:
            _live(args, report)
        else:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *CONTROLLED, "-q"],
                cwd=ROOT,
                capture_output=True,
                check=False,
            )
            report["controlled"] = "passed" if result.returncode == 0 else "failed"
            status = 0 if result.returncode == 0 else 1
    except Exception:  # noqa: BLE001 - 真实验收不输出认证/HTTP/配置异常原文
        report["failure"] = "验收未完成；请在产品内核对配置、提案或运行记录"
        status = 1
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(encoded)
    print(encoded, end="")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
