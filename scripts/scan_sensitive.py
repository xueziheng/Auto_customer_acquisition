"""敏感内容扫描 —— 只匹配高置信凭证形态，绝不输出命中内容。

用途：防止 DSN/密码/Token/私有密钥/环境变量值进入受跟踪文件或输出。
- 默认扫描 tracked 文件（``git ls-files``）；``--staged`` 读取 git index blob
  （``git cat-file blob :path``，不读工作树）；也可显式传路径。
- 覆盖 .py/.md/.toml/.yaml/.yml/.json/.ts/.vue/.sh/.txt/.ini/.cfg/.conf 与
  Makefile/Dockerfile/.env/.env.* 等文本；跳过缓存/构建目录。
- 只输出 ``path:line:kind``，从不输出匹配文本；Finding.path 始终为仓库相对路径。
- 显式路径统一仓库边界校验：仓库内相对或仓库内绝对路径可扫描；绝对越界、
  含 ``..`` 越界、解析后指向仓库外的 symlink 均在读取前拒绝——CLI 固定错误、
  exit 1，不输出外部路径或内容。
- git 发现/读取失败 fail closed：退出 1，不泄露 git stderr 或文件内容。
- 允许文档占位符（``<your-token>``、``xxx``、重复 X 形、scheme-only ``postgresql://`` 等）。
- 退出码：0 干净；1 命中。
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
# 缓存/构建目录一律跳过；.env 不在此列（必须扫描）。
_IGNORED_PARTS = {
    "node_modules",
    "__pycache__",
    ".git",
    "dist",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "volumes",
}
_SCAN_SUFFIXES = {
    ".py", ".md", ".toml", ".yaml", ".yml", ".json", ".ts", ".vue",
    ".sh", ".txt", ".ini", ".cfg", ".conf",
}
_RELEVANT_BASENAMES = frozenset({"Makefile", "makefile", "GNUmakefile", "Dockerfile", "dockerfile"})

# 占位符形态（允许文档占位，不视为命中）：<...>、xxx、重复 X、常见占位词、4+ 相同字符。
_PLACEHOLDER_RE = re.compile(
    r"^(?:<[^>]*>|x{3,}|X{3,}|your[a-z_]*|changeme|change_me|example|dummy|"
    r"secret|redacted|placeholder|\.\.\.|todo|token|password|passwd|pass|(.)\1{3,})$",
    re.IGNORECASE,
)

# 既有文档中的明确占位描述（精确 token，非通配豁免）：
#   tradeos_local —— phase0 计划的本地 dev 示例密码；
#   非占位       —— slice3 计划对 scanner 行为的中文描述。
_DOC_PLACEHOLDER_TOKENS = frozenset({"tradeos_local", "非占位"})


def _is_placeholder(value: str) -> bool:
    """匹配文本是否仅占位（剥引号/空白后）。

    只做精确判断：已知文档占位 token、或通用占位形态。**不**做 localhost/
    非 ASCII 通配豁免——真实本地凭证与 Unicode 密码仍应命中。
    """
    stripped = value.strip().strip("'\"")
    if not stripped:
        return True
    if stripped in _DOC_PLACEHOLDER_TOKENS:
        return True
    return bool(_PLACEHOLDER_RE.match(stripped))


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str


# (kind, regex, placeholder_group|None)：None 表示整段匹配都是敏感值。
# password 不带 \\b：config 里常见 ``XXX_PASSWORD=`` 前缀；val 字符集排除反斜杠，
# 避免把源码中的 ``\\n`` 转义吸收进 val 造成假阳性。
_PATTERNS: list[tuple[str, re.Pattern[str], str | None]] = [
    ("dsn-userinfo", re.compile(r"\b[a-z][a-z0-9+.-]*://(?P<user>[^\s/@:]+):(?P<pass>[^\s/@]+)@"), "pass"),
    ("aws-access-key", re.compile(r"\bAKIA(?P<key>[0-9A-Z]{16})\b"), "key"),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"), None),
    ("github-token", re.compile(r"\bgh[psu]_[A-Za-z0-9]{36}\b"), None),
    ("openai-token", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), None),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), None),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), None),
    (
        "tavily-api-key",
        re.compile(
            r"(?<![A-Za-z0-9_-])tvly-(?:dev|prod)-(?P<key>[A-Za-z0-9_-]{20,})"
            r"(?![A-Za-z0-9_*-]|\.\.\.)"
        ),
        "key",
    ),
    (
        "hunter-api-key",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_-])(?:hunter_api_key|X-API-KEY)"
            r"(?![A-Za-z0-9_-])['\"]?\s*[:=]\s*['\"]?"
            r"(?P<val>[A-Za-z0-9_-]{20,})"
        ),
        "val",
    ),
    (
        "password-assignment",
        re.compile(r"(?i)password\s*=\s*(?P<val>['\"]?[^\s'\"`\\=,;，。；、：！？…（）【】《》]+)"),
        "val",
    ),
]


def _runtime_value(node: ast.AST, *, argument: bool = False) -> bool:
    """识别运行期表达式；包装函数中的硬编码字符串仍须拦截。"""
    if isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        return _runtime_value(node.value)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return _is_placeholder(node.value)
        return node.value is None or node.value is Ellipsis or argument
    if isinstance(node, ast.Await):
        return _runtime_value(node.value)
    if isinstance(node, ast.IfExp):
        return _runtime_value(node.body) and _runtime_value(node.orelse)
    if isinstance(node, ast.Lambda):
        return _runtime_value(node.body)
    if isinstance(node, ast.Subscript):
        return _runtime_value(node.value)
    if isinstance(node, ast.Call):
        function = ast.unparse(node.func)
        if function == "getpass.getpass":
            return True  # 参数是提示文案，返回值由终端读取。
        if function in {"str", "bytes", "SecretStr"} and any(
            isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float))
            for arg in node.args
        ):
            return False
        return all(_runtime_value(arg, argument=True) for arg in node.args) and all(
            _runtime_value(item.value, argument=True) for item in node.keywords
        )
    if isinstance(node, ast.BinOp):
        return _runtime_value(node.left, argument=argument) and _runtime_value(
            node.right, argument=argument
        )
    return False


def _python_runtime_spans(text: str, *, embedded: bool = False) -> list[tuple[int, int, int, int]]:
    """仅对可解析的 Python 建立豁免区间；语法失败保留原始扫描。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    spans = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign, ast.keyword))
            and node.value is not None
            and _runtime_value(node.value)
        ):
            spans.append((node.lineno, node.col_offset, node.end_lineno or node.lineno,
                          node.end_col_offset or node.col_offset))
        if (
            not embedded and isinstance(node, ast.Constant)
            and isinstance(node.value, str) and "\n" in node.value
            and "\\" not in (ast.get_source_segment(text, node) or "")
        ):
            for a, b, c, d in _python_runtime_spans(node.value, embedded=True):
                # 首行列位置受字符串引号影响，保守地只处理后续完整代码行。
                if a > 1:
                    spans.append((node.lineno + a - 1, b, node.lineno + c - 1, d))
    return spans


def scan_text(text: str, *, suffix: str = "") -> list[tuple[int, str]]:
    """返回 ``(line, kind)`` 列表；占位形态跳过，不输出匹配内容。"""
    findings: list[tuple[int, str]] = []
    spans = _python_runtime_spans(text) if suffix == ".py" else []
    for lineno, line in enumerate(text.splitlines(), 1):
        for kind, regex, placeholder_group in _PATTERNS:
            for match in regex.finditer(line):
                value = match.group(placeholder_group) if placeholder_group else match.group(0)
                if _is_placeholder(value):
                    continue
                if kind == "password-assignment" and suffix == ".py":
                    position = (lineno, len(line[:match.start()].encode("utf-8")))
                    if any((a, b) <= position < (c, d) for a, b, c, d in spans):
                        continue
                if kind == "password-assignment" and (
                    (suffix in {".vue", ".ts", ".js"} and re.match(r'''ref\(["']["']\)''', line[match.start("val"):]))
                    or (suffix == ".md" and re.fullmatch(r"SecretStr\(\.\.\.\)\)?", value))
                ):
                    continue
                # 只接受 f-string 内完整的变量插值；静态前后缀与 .env 不豁免。
                if kind == "dsn-userinfo" and suffix == ".py" and re.fullmatch(r"\{[A-Za-z_]\w*\}", value):
                    try:
                        parsed = ast.parse(line.strip())
                    except SyntaxError:
                        parsed = ast.Module(body=[], type_ignores=[])
                    column = len(line[:match.start()].lstrip().encode("utf-8"))
                    if any(
                        isinstance(node, ast.JoinedStr)
                        and node.col_offset <= column < (node.end_col_offset or 0)
                        for node in ast.walk(parsed)
                    ):
                        continue
                findings.append((lineno, kind))
    return findings


def _resolve_repo_file(path: Path) -> tuple[Path, Path]:
    """把显式路径解析为仓库内文件；越界/外链 symlink 抛 ``_PathError``（读取前拒绝）。

    仓库内相对路径或仓库内绝对路径均可扫描；返回 (仓库内绝对路径, 仓库相对路径)。
    """
    full = path.resolve() if path.is_absolute() else (_REPO_ROOT / path).resolve()
    root = _REPO_ROOT.resolve()
    if full == root or not full.is_relative_to(root):
        raise _PathError("路径越界或指向仓库外")
    return full, full.relative_to(root)


def scan_file(path: Path) -> list[Finding]:
    """扫描单个文件（仓库内相对或仓库内绝对路径）。

    越界/外链 symlink 抛 ``_PathError``；Finding.path 始终为仓库相对路径。
    """
    full, rel = _resolve_repo_file(path)
    try:
        content = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [Finding(rel.as_posix(), line, kind) for line, kind in scan_text(content, suffix=rel.suffix)]


def _is_relevant(path: Path) -> bool:
    if any(part in _IGNORED_PARTS or part.endswith(".egg-info") for part in path.parts):
        return False
    name = path.name
    if name in _RELEVANT_BASENAMES or name.startswith("Dockerfile."):
        return True
    if name == ".env" or name.startswith(".env."):
        return True
    return path.suffix.lower() in _SCAN_SUFFIXES


class _GitError(RuntimeError):
    """git 不可用或命令失败：fail closed（不泄露 stderr/内容）。"""


class _PathError(RuntimeError):
    """显式路径越界或指向仓库外：读取前拒绝，不输出外部路径或内容。"""


def _git(args: list[str]) -> list[str]:
    try:
        result = subprocess.run(
            ["git", *args], capture_output=True, text=True, check=False, cwd=_REPO_ROOT
        )
    except OSError:
        raise _GitError("git 不可用") from None
    if result.returncode != 0:
        raise _GitError("git 命令失败") from None
    return [line for line in result.stdout.splitlines() if line.strip()]


def discover_tracked() -> list[Path]:
    return [Path(p) for p in _git(["ls-files"])]


def discover_staged() -> list[Path]:
    """仅含 index 中仍有 blob 的路径（``--diff-filter`` 排除 staged 删除）。"""
    return [Path(p) for p in _git(["diff", "--cached", "--name-only", "--diff-filter=ACMRT"])]


def read_index_blob(rel: str) -> str | None:
    """读取 index blob；二进制/不可解码返回 None；git 失败 fail closed。"""
    try:
        result = subprocess.run(
            ["git", "cat-file", "blob", f":{rel}"],
            capture_output=True,
            check=False,
            cwd=_REPO_ROOT,
        )
    except OSError:
        raise _GitError("git 不可用") from None
    if result.returncode != 0:
        raise _GitError("git 命令失败") from None
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _scan_repo_safe(rel: Path) -> list[Finding]:
    """扫描 git 派生的仓库相对路径：越界/外链 symlink 拒绝读取并跳过。"""
    try:
        full, rel_resolved = _resolve_repo_file(rel)
    except _PathError:
        return []
    try:
        content = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [Finding(rel_resolved.as_posix(), line, kind) for line, kind in scan_text(content, suffix=rel_resolved.suffix)]


def _scan_explicit(paths: list[Path]) -> list[Finding]:
    """扫描显式路径：先统一校验边界（任一越界即 fail closed），再扫描仓库内文件。"""
    resolved = [_resolve_repo_file(p) for p in paths]
    findings: list[Finding] = []
    for full, rel in resolved:
        if not _is_relevant(rel):
            continue
        try:
            content = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        findings.extend(Finding(rel.as_posix(), line, kind) for line, kind in scan_text(content, suffix=rel.suffix))
    return findings


def _scan_tracked() -> list[Finding]:
    findings: list[Finding] = []
    for rel in discover_tracked():
        if not _is_relevant(rel):
            continue
        findings.extend(_scan_repo_safe(rel))
    return findings


def _scan_staged() -> list[Finding]:
    findings: list[Finding] = []
    for rel in discover_staged():
        if not _is_relevant(rel):
            continue
        if rel.is_absolute() or ".." in rel.parts:
            continue
        content = read_index_blob(rel.as_posix())
        if content is None:
            continue
        findings.extend(Finding(rel.as_posix(), line, kind) for line, kind in scan_text(content, suffix=rel.suffix))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="敏感内容扫描：只匹配高置信凭证形态")
    parser.add_argument("paths", nargs="*", help="显式路径（覆盖 git 模式）")
    parser.add_argument("--staged", action="store_true", help="扫描 index 中的暂存 blob（不读工作树）")
    parser.add_argument("--quiet", action="store_true", help="只输出违规，不输出汇总")
    args = parser.parse_args(argv)

    try:
        if args.paths:
            findings = _scan_explicit([Path(p) for p in args.paths])
        elif args.staged:
            findings = _scan_staged()
        else:
            findings = _scan_tracked()
    except _GitError:
        print("敏感扫描失败：git 发现/读取异常（fail closed）。", file=sys.stderr)
        return 1
    except _PathError:
        print("敏感扫描失败：路径越界或指向仓库外（已拒绝）。", file=sys.stderr)
        return 1

    for finding in findings:
        print(f"{finding.path}:{finding.line}:{finding.kind}")
    if findings and not args.quiet:
        print(f"发现 {len(findings)} 处高置信凭证形态。")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
