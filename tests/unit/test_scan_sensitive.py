"""S3-1 敏感扫描单测。

行为断言：
- 只匹配高置信凭证形态：带非占位 userinfo 的 DSN、AWS AKIA、PEM 私钥头、
  GitHub token、``password=`` 非占位赋值。
- **合成秘密运行时拼接**：测试源码不写完整 assignment/DSN/凭证字面量。
- 允许文档占位符（``<...>``、xxx、重复 X 形 AKIA、scheme-only ``postgresql://``）。
- 覆盖 .env/.env.*/Makefile 等文本；``--staged`` 读 index blob 而非工作树。
- 结果只输出 path/line/kind，绝不输出命中内容。
- git 发现/读取失败 fail closed（CLI exit 1，不泄露 git stderr/内容）。
- CLI 退出码 0（干净）/1（命中）；``--quiet`` 只输违规。

RED：``scripts/scan_sensitive.py`` 尚未创建 → 动态加载 pytest.fail / 子进程非零。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "scan_sensitive.py"


def _load_scanner():
    """动态加载脚本模块；缺失转行为失败（RED，非收集错误）。"""
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    import importlib.util

    spec = importlib.util.spec_from_file_location("scan_sensitive", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_sensitive"] = module  # dataclass 处理需按 cls.__module__ 查 sys.modules
    spec.loader.exec_module(module)
    return module


# --- 运行时拼接的合成秘密（不写完整字面量） ------------------------------------


def _dsn_secret() -> str:
    pw = "h" + "unter" + "2x"  # -> "hunter2x"
    return "postgresql://admin:" + pw + "@db.example.com:5432/tradeos"


def _aws_secret() -> str:
    return "AKIA" + "PQRS" + "TUVW" + "XYZW" + "1234"  # -> AKIA + 16


def _gh_secret() -> str:
    return "ghp_" + "A1B2C3D4E" + "F6G7H8I9J" + "0K1L2M3N4" + "O5P6Q7R8S"  # -> 4+36


def _pem_secret() -> str:
    head = "-----BEGIN " + "PRIVATE " + "KEY-----"
    body = "MIIB" + "AQAB" + "AA" + "=="
    tail = "-----END " + "PRIVATE " + "KEY-----"
    return f"{head}\n{body}\n{tail}"


def _password_secret() -> str:
    return "sup" + "er" + "secret"  # -> "supersecret"


def _hunter_key_canary() -> str:
    """无前缀 Hunter-shaped canary；只在运行时拼接，仓库不保存可运行值。"""
    return "a1b2c3d4e5" + "f60718293a" + "4b5c6d7e8f" + "9012345678"


def _placeholder_dsn() -> str:
    """占位 DSN 形态：源码不写完整 DSN，运行时拼接。"""
    return "postgresql://<" + "user" + ">:<" + "pass" + ">@host:5432/db"


def _placeholder_password() -> str:
    return "<your-" + "password" + ">"


# --- 匹配与占位符 -------------------------------------------------------------


def test_dsn_userinfo_flagged_and_not_leaked() -> None:
    module = _load_scanner()
    findings = module.scan_text(_dsn_secret())
    kinds = {kind for _, kind in findings}
    assert "dsn-userinfo" in kinds
    for _line, kind in findings:
        assert "hunter2x" not in kind  # 命中内容绝不进输出


def test_scheme_only_and_placeholders_allowed() -> None:
    module = _load_scanner()
    sample = (
        'url = "postgresql://"  # scheme-only（conftest 同款，允许）\n'
        "dsn = " + _placeholder_dsn() + "  # 占位\n"
        "password = " + _placeholder_password() + "\n"
        'token = "ghp_xxxx...xxxx"  # 占位\n'
        'aws = "AKIAXXXXXXXXXXXXXXXX"  # 占位（重复 X）\n'
    )
    assert module.scan_text(sample) == []


def test_aws_pem_password_token_flagged() -> None:
    module = _load_scanner()
    text = "\n".join([_aws_secret(), _pem_secret(), _gh_secret(), "password = " + _password_secret()])
    kinds = {kind for _, kind in module.scan_text(text)}
    assert {"aws-access-key", "private-key", "github-token", "password-assignment"} <= kinds


def test_placeholder_detection() -> None:
    module = _load_scanner()
    assert module._is_placeholder("<your-password>")
    assert module._is_placeholder("xxx")
    assert module._is_placeholder("XXXXXXXXXXXXXXXX")
    assert module._is_placeholder("pass")  # 字面占位词
    assert not module._is_placeholder("hunter2x")


def test_scan_file_returns_repo_relative_path_line_kind() -> None:
    """仓库内绝对路径可扫描；Finding.path 输出仓库相对路径。"""
    module = _load_scanner()
    with tempfile.TemporaryDirectory(dir=_REPO_ROOT) as td:
        tmp = Path(td) / "f.py"
        tmp.write_text("x = 1\naws = " + _aws_secret() + "\n", encoding="utf-8")
        findings = module.scan_file(tmp)  # 仓库内绝对路径
        assert findings
        f = findings[0]
        rel = tmp.resolve().relative_to(_REPO_ROOT.resolve()).as_posix()
        assert f.path == rel  # 仓库相对路径
        assert f.line == 2
        assert f.kind == "aws-access-key"


# --- CLI 行为 ------------------------------------------------------------------


def _run_cli(paths: list[str], *, quiet: bool = False):
    cmd = [sys.executable, str(_SCRIPT), *paths]
    if quiet:
        cmd.append("--quiet")
    return subprocess.run(
        cmd, capture_output=True, text=True, cwd=_REPO_ROOT, check=False
    )


def test_cli_exit_one_and_output_never_leaks() -> None:
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    with tempfile.TemporaryDirectory(dir=_REPO_ROOT) as td:
        tmp = Path(td) / "leak.py"
        tmp.write_text("dsn = " + _dsn_secret() + "\n", encoding="utf-8")
        result = _run_cli([str(tmp)])  # 仓库内绝对路径
        assert result.returncode == 1
        assert "dsn-userinfo" in result.stdout
        assert "hunter2x" not in result.stdout
        assert "hunter2x" not in result.stderr


def test_hunter_validation_json_and_log_canaries_report_only_fixed_metadata() -> None:
    """验证相关 JSON/log 中的 Hunter key 只能产生固定 path/line/category。"""
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    canary = _hunter_key_canary()
    with tempfile.TemporaryDirectory(dir=_REPO_ROOT) as td:
        tmp = Path(td) / "provider-validation.json"
        tmp.write_text(
            '{"event":"provider_validation_failed","hunter_api_key":"'
            + canary
            + '","outcome":"auth_required"}\n'
            + "provider=hunter operation=validation X-API-KEY="
            + canary
            + " outcome=provider_transient\n",
            encoding="utf-8",
        )
        result = _run_cli([str(tmp)], quiet=True)
        rel = tmp.resolve().relative_to(_REPO_ROOT.resolve()).as_posix()

        assert result.returncode == 1
        assert result.stderr == ""
        assert result.stdout.splitlines() == [
            f"{rel}:1:hunter-api-key",
            f"{rel}:2:hunter-api-key",
        ]
        assert canary not in result.stdout + result.stderr


def test_hunter_key_field_case_separator_boundaries_and_placeholders() -> None:
    module = _load_scanner()
    canary = _hunter_key_canary()
    findings = module.scan_text(
        '"HUNTER_API_KEY" : "'
        + canary
        + '"\nX-API-KEY: '
        + canary
        + "\nnot_hunter_api_key="
        + canary
        + "\nX-API-KEY-NOTE="
        + canary
        + '\n{"hunter_api_key":"<your-hunter-api-key>"}\n'
    )

    assert findings == [(1, "hunter-api-key"), (2, "hunter-api-key")]


def test_cli_clean_exit_zero() -> None:
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    with tempfile.TemporaryDirectory(dir=_REPO_ROOT) as td:
        rel = Path(td).name + "/clean.py"  # 仓库内相对路径
        tmp = _REPO_ROOT / rel
        tmp.write_text("x = 1\n# postgresql:// scheme-only 允许\n", encoding="utf-8")
        result = _run_cli([rel])
        assert result.returncode == 0
        assert result.stdout.strip() == ""


def test_cli_quiet_prints_only_violations() -> None:
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    with tempfile.TemporaryDirectory(dir=_REPO_ROOT) as td:
        tmp = Path(td) / "v.py"
        tmp.write_text("gh = " + _gh_secret() + "\n", encoding="utf-8")
        result = _run_cli([str(tmp)], quiet=True)  # 仓库内绝对路径
        assert result.returncode == 1
        lines = result.stdout.strip().splitlines()
        assert all(":" in line for line in lines)  # 每行 path:line:kind
        assert _gh_secret() not in result.stdout


def test_cli_rejects_absolute_path_outside_repo(tmp_path: Path) -> None:
    """绝对越界：读取前拒绝，CLI exit 1，外部秘密与路径不泄。"""
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    ext = tmp_path / "outside_secret.py"  # 外部目标（pytest tmp_path，非扫描对象）
    ext.write_text("aws = " + _aws_secret() + "\n", encoding="utf-8")
    result = _run_cli([str(ext)])
    assert result.returncode == 1
    assert result.stdout.strip() == ""
    assert result.stderr.strip() != ""  # 固定错误
    assert "aws-access-key" not in result.stdout + result.stderr
    assert _aws_secret() not in result.stdout + result.stderr
    assert str(ext) not in result.stdout + result.stderr  # 外部路径不输出


def test_cli_rejects_relative_parent_escape() -> None:
    """相对 ``..`` 越界：读取前拒绝（文件不存在也拒绝），CLI exit 1。"""
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    rel = "../definitely-not-readable-by-scanner.txt"
    result = _run_cli([rel])
    assert result.returncode == 1
    assert result.stdout.strip() == ""
    assert result.stderr.strip() != ""
    assert rel not in result.stdout + result.stderr  # 外部路径不输出


def test_cli_rejects_symlink_pointing_outside_repo(tmp_path: Path) -> None:
    """symlink 外链：仓库内 symlink 指向仓库外 → 读取前拒绝，CLI exit 1。"""
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    ext = tmp_path / "outside_secret.py"  # 外部目标
    ext.write_text("aws = " + _aws_secret() + "\n", encoding="utf-8")
    with tempfile.TemporaryDirectory(dir=_REPO_ROOT) as td:
        link = Path(td) / "evil.py"  # symlink 本身在仓库内临时目录
        link.symlink_to(ext)
        result = _run_cli([str(link)])
        assert result.returncode == 1
        assert result.stdout.strip() == ""
        assert result.stderr.strip() != ""
        assert _aws_secret() not in result.stdout + result.stderr
        assert str(ext) not in result.stdout + result.stderr


def test_self_scan_clean() -> None:
    """self-scan 回归：scanner 与测试文件自身不得命中任何凭证形态。"""
    if not _SCRIPT.exists():
        pytest.fail(f"RED：{_SCRIPT} 尚未创建")
    result = _run_cli([str(_SCRIPT), str(Path(__file__))])
    assert result.returncode == 0, result.stdout


# --- 回归：真实本地凭证与 Unicode 密码仍命中；文档占位 token 精确通过 ---------------


def test_localhost_real_credential_still_flagged() -> None:
    module = _load_scanner()
    pw = "loc" + "al" + "pw"  # -> "localpw"
    dsn = "postgresql://admin:" + pw + "@localhost:5432/db"
    kinds = {kind for _, kind in module.scan_text(dsn)}
    assert "dsn-userinfo" in kinds  # 本地主机 + 真实密码**不**豁免


def test_unicode_real_password_still_flagged() -> None:
    module = _load_scanner()
    line = "password = " + "密" + "码" + "123"  # 真实 Unicode 密码
    kinds = {kind for _, kind in module.scan_text(line)}
    assert "password-assignment" in kinds  # 非占位 token 不豁免


def test_prefixed_password_env_key_still_flagged() -> None:
    """``XXX_PASSWORD=`` 前缀键（.env 常见形态）也要命中。"""
    module = _load_scanner()
    line = "DB_PASSWORD=" + _password_secret()
    kinds = {kind for _, kind in module.scan_text(line)}
    assert "password-assignment" in kinds


def test_documented_placeholder_tokens_allowed() -> None:
    module = _load_scanner()
    sample = (
        # phase0 计划本地 dev 示例密码（精确占位 token：tradeos_local）
        "postgresql+asyncpg://" + "tradeos:" + "trade" + "os_local" + "@localhost:5432/tradeos\n"
        # slice3 计划对 scanner 行为的中文描述（精确占位 token：非占位）
        "password=" + "非" + "占位" + "\n"
    )
    assert module.scan_text(sample) == []


def test_hunter_docs_and_fixtures_contain_refs_without_runnable_secrets() -> None:
    """受跟踪文档/fixture 只保存引用名与占位符，扫描结果必须为空。"""
    module = _load_scanner()
    tracked = set(module.discover_tracked())
    expected = {
        Path("infra/.env.example"): "TRADEOS_HUNTER_API_KEY_SECRET_REF",
        Path("docs/operations/hunter-provider-readiness.md"):
            "TRADEOS_HUNTER_API_KEY_SECRET_REF",
        Path("docs/superpowers/specs/2026-08-25-hunter-provider-readiness-design.md"):
            "TRADEOS_HUNTER_API_KEY_SECRET_REF",
        Path("tests/integration/test_provider_validation_gateway.py"): "DEPLOYMENT_REF",
    }

    for path, reference_name in expected.items():
        assert path in tracked
        text = (_REPO_ROOT / path).read_text(encoding="utf-8")
        assert reference_name in text
        assert module.scan_file(path) == []


def _fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(
        rf"```{re.escape(language)}\n(.*?)\n```",
        text,
        flags=re.DOTALL,
    )


def _selected_columns(sql: str) -> tuple[str, ...]:
    match = re.search(
        r"\ASELECT\s+(.*?)\s+FROM\s+",
        sql.strip(),
        flags=re.DOTALL | re.IGNORECASE,
    )
    assert match is not None
    return tuple(" ".join(column.split()) for column in match.group(1).split(","))


def test_hunter_runbook_pins_runtime_outcome_queries_and_exact_ready_copy() -> None:
    """运维文档必须使用受控 Python、双层结果查询与前端真实 ready 文案。"""
    runbook = (
        _REPO_ROOT / "docs/operations/hunter-provider-readiness.md"
    ).read_text(encoding="utf-8")

    assert "TRADEOS_PYTHON_BIN" in runbook
    assert "Python 3.12" in runbook
    assert "/Users/" not in runbook
    assert "CODEX_HOME" not in runbook
    assert "$HOME" not in runbook
    assert '"$TRADEOS_PYTHON_BIN" scripts/run_alembic.py heads' in runbook
    assert (
        '"$TRADEOS_PYTHON_BIN" scripts/configure_hunter_provider.py' in runbook
    )
    assert '"$TRADEOS_PYTHON_BIN" scripts/validate_hunter_provider.py' in runbook
    assert "WHERE tenant_id = :tenant_id" in runbook
    assert "AND validation_key = :validation_key" in runbook
    assert "AND idempotency_key = :validation_key" in runbook
    assert "outcome_code" in runbook
    assert "retry_after_at" in runbook
    assert "CLI 安全 category" in runbook
    assert "durable readiness outcome" in runbook
    assert "provider_auth_required" in runbook
    assert "CLI 不会直接输出 `auth_required` 或 `response_invalid`" in runbook
    validation_section = runbook.split(
        "## 4. 显式执行一次 provider.hunter.validate", maxsplit=1
    )[1].split("## 5. 处理固定结果", maxsplit=1)[0]
    validation_queries = _fenced_blocks(validation_section, "sql")
    assert len(validation_queries) == 2
    assert _selected_columns(validation_queries[0]) == (
        "event_type",
        "outcome_code",
        "occurred_at",
    )
    assert _selected_columns(validation_queries[1]) == (
        "tool_call_id",
        "status",
        "error_category",
        "retry_after_at",
        "created_at",
        "updated_at",
        "completed_at",
    )
    for query in validation_queries:
        assert "select *" not in query.lower()
        projection = " ".join(_selected_columns(query)).lower()
        for forbidden in (
            "*",
            "configuration_hash",
            "secret_ref",
            "response",
            "payload",
            "credential",
            "key",
        ):
            assert forbidden not in projection
    assert (
        "联系人补全生产组合已就绪；每个目标国家仍会逐次检查国家政策。"
        in runbook
    )


def test_hunter_runbook_bootstrap_stops_after_wrong_python(tmp_path: Path) -> None:
    """版本探针失败时不得继续执行 Alembic 或安全门命令。"""
    runbook = (
        _REPO_ROOT / "docs/operations/hunter-provider-readiness.md"
    ).read_text(encoding="utf-8")
    bootstrap = _fenced_blocks(runbook, "bash")[0]
    marker = tmp_path / "unexpected-later-command"
    fake_python = tmp_path / "tradeos-fake-python"
    fake_python.write_text(
        """#!/bin/sh
if [ "${1:-}" = "-c" ]; then
  exit 23
fi
printf '%s\n' "$*" >> "$TRADEOS_TEST_MARKER"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", bootstrap],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "TRADEOS_PYTHON_BIN": str(fake_python),
            "TRADEOS_TEST_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 23
    assert not marker.exists()


# --- 默认 tracked / --staged 文件发现（运行时临时 git 仓库 + monkeypatch） ----------


def _init_git(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)


def _make_temp_git_repo(module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """运行时建临时 git 仓库：tracked 两文件已提交、staged 一文件已 add 未提交、
    untracked 一文件未 add；monkeypatch 模块 _REPO_ROOT 指向该仓库。"""
    _init_git(tmp_path)
    (tmp_path / "tracked_secret.py").write_text("gh = " + _gh_secret() + "\n", encoding="utf-8")
    (tmp_path / "tracked_clean.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked_secret.py", "tracked_clean.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    (tmp_path / "staged_secret.py").write_text("aws = " + _aws_secret() + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged_secret.py"], cwd=tmp_path, check=True)
    (tmp_path / "untracked_secret.py").write_text("dsn = " + _dsn_secret() + "\n", encoding="utf-8")
    monkeypatch.setattr(module, "_REPO_ROOT", tmp_path)


def test_default_mode_scans_tracked_not_untracked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_scanner()
    _make_temp_git_repo(module, tmp_path, monkeypatch)
    tracked = module.discover_tracked()
    names = {p.as_posix() for p in tracked}
    assert "tracked_secret.py" in names
    assert "tracked_clean.py" in names
    assert "untracked_secret.py" not in names

    findings = module._scan_tracked()
    kinds = {f.kind for f in findings}
    assert "github-token" in kinds
    assert not any(f.path.endswith("untracked_secret.py") for f in findings)


def test_staged_mode_reads_index_blob_not_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_scanner()
    _make_temp_git_repo(module, tmp_path, monkeypatch)
    staged = module.discover_staged()
    names = {p.as_posix() for p in staged}
    assert names == {"staged_secret.py"}  # 仅 cached name-only

    # worktree 已清理（文件被删除），index 仍持有秘密 blob —— 必须仍拦截。
    (tmp_path / "staged_secret.py").unlink()

    findings = module._scan_staged()
    kinds = {f.kind for f in findings}
    assert "aws-access-key" in kinds
    assert all(f.path == "staged_secret.py" for f in findings)
    assert not any(f.path.endswith("tracked_secret.py") for f in findings)


def test_env_and_makefile_scanned_when_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env/.env.*/Makefile 文本须纳入扫描，且 stage 后经 index blob 命中。"""
    module = _load_scanner()
    _init_git(tmp_path)
    (tmp_path / ".env").write_text("DB_PASSWORD=" + _password_secret() + "\n", encoding="utf-8")
    (tmp_path / ".env.prod").write_text("S3_KEY=" + _aws_secret() + "\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("export CI_TOKEN=" + _gh_secret() + "\n", encoding="utf-8")
    subprocess.run(["git", "add", ".env", ".env.prod", "Makefile"], cwd=tmp_path, check=True)
    monkeypatch.setattr(module, "_REPO_ROOT", tmp_path)

    findings = module._scan_staged()
    kinds = {f.kind for f in findings}
    paths = {f.path for f in findings}
    assert "password-assignment" in kinds  # .env 的 DB_PASSWORD
    assert "aws-access-key" in kinds  # .env.prod 的 S3_KEY
    assert "github-token" in kinds  # Makefile 的 CI_TOKEN
    assert paths == {".env", ".env.prod", "Makefile"}


def test_fail_closed_on_git_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """git 发现/读取失败须 fail closed：exit 1，不泄露 git stderr/内容。"""
    module = _load_scanner()

    def _boom(*args):
        raise module._GitError("git unavailable")

    monkeypatch.setattr(module, "_git", _boom)
    assert module.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    assert "git unavailable" not in captured.out + captured.err
