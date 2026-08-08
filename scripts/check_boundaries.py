#!/usr/bin/env python3
"""结构边界自检 —— 守 AGENTS.md 的九条硬边界里可机检的那几条。

用法：
    python scripts/check_boundaries.py            # 常规检查
    python scripts/check_boundaries.py --skeleton # 追加骨架期检查（stub 纯度）
    python scripts/check_boundaries.py --quiet    # 只输出违规

在 CI 和每次提交前跑。它拦的是「架构腐化」，不是「代码写错」——
后者归测试管。

有意不做的检查：
- 业务正确性（归单测）
- 类型正确性（归 mypy）
- 代码风格（归 ruff）
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 分层。数字越大越上层；上层可依赖下层，反向即违规（硬边界 9）。
LAYERS = {
    "shared": 0,
    "domains": 1,
    "tool_gateway": 2,
    "notification_gateway": 2,
    "artifact_store": 2,
    "connectors": 2,
    "agent_runtime": 3,
    "workflows": 3,
    "apps": 4,
    "tests": 5,
    "scripts": 5,
}

# 域和横切层都不许直接用外部 SDK；外部调用只经 connectors + tool_gateway。
FORBIDDEN_SDK = {
    "httpx", "requests", "openai", "anthropic", "sqlalchemy", "asyncpg",
    "playwright", "boto3", "redis", "aiohttp", "psycopg", "psycopg2",
}
SDK_ALLOWED_IN = {"connectors", "apps", "tests", "scripts"}

# 金额字段名片段：这些字段用 float 是硬边界 2 违规。
# 不收 "total"/"value" 这类泛词——total_score、queue_depth 不是钱，
# 会喊狼来了的检查最后没人看。
MONEY_HINTS = (
    "price", "cost", "amount", "margin", "fee", "profit",
    "revenue", "budget", "salary", "payment",
)
# 命中 MONEY_HINTS 但其实不是钱的：比率、计数、分数。
MONEY_EXCLUDE = ("score", "count", "ratio", "_rate", "rate_", "percent", "days")

# 需要有 AGENTS.md 的目录（就近生效约定的落地）。
AGENTS_REQUIRED = [
    "shared", "domains", "tool_gateway", "notification_gateway",
    "artifact_store", "agent_runtime", "skills", "connectors",
    "workflows", "apps", "tests", "apps/api", "connectors/gmail",
]

# 每个域必须齐的七件套。
DOMAIN_FILES = (
    "AGENTS.md", "models.py", "schemas.py", "service.py",
    "repository.py", "events.py", "errors.py",
)


@dataclass
class Finding:
    rule: str
    path: str
    line: int | None
    detail: str

    def __str__(self) -> str:
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"  [{self.rule}] {loc}\n      {self.detail}"


def py_files(root: Path = REPO):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(REPO)
        if any(
            part.startswith((".", "_"))
            and part not in ("__init__.py",)
            or part in ("__pycache__", "node_modules", "volumes")
            for part in rel.parts[:-1]
        ):
            continue
        if p.name.startswith("._"):
            continue
        yield p


def imported_modules(tree: ast.AST):
    """产出 (模块名, 行号)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def check_layering(findings: list[Finding]) -> None:
    """硬边界 9：依赖单向 + 域间零依赖 + 不碰他域内部。"""
    for path in py_files():
        rel = path.relative_to(REPO)
        top = rel.parts[0]
        if top not in LAYERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            findings.append(Finding("parse", str(rel), None, str(exc)))
            continue

        own_domain = rel.parts[1] if top == "domains" and len(rel.parts) > 2 else None

        for mod, line in imported_modules(tree):
            mtop = mod.split(".")[0]

            if mtop in LAYERS and mtop != top and LAYERS[mtop] > LAYERS[top]:
                findings.append(Finding(
                    "reverse-dep", str(rel), line,
                    f"{top} 导入了上层 {mod}；依赖只能向下（硬边界 9）",
                ))

            if own_domain and mod.startswith("domains."):
                other = mod.split(".")[1]
                if other != own_domain:
                    findings.append(Finding(
                        "cross-domain", str(rel), line,
                        f"域 {own_domain} 直接导入域 {other}；"
                        f"改走 shared/events 或对方 service.py",
                    ))

            if top != "domains" and re.match(r"domains\.\w+\.(models|repository)$", mod):
                findings.append(Finding(
                    "domain-internals", str(rel), line,
                    f"{mod} 是域私有实现；跨域只能用 schemas.py 与 service.py",
                ))

            if mtop in FORBIDDEN_SDK and top not in SDK_ALLOWED_IN:
                findings.append(Finding(
                    "external-sdk", str(rel), line,
                    f"{top} 层不得直接用 {mtop}；外部调用经 connectors + tool_gateway",
                ))


def check_money_float(findings: list[Finding]) -> None:
    """硬边界 2：金额禁止 float。"""
    pattern = re.compile(r"^\s*(\w+)\s*:\s*(float|float\s*\|\s*None)\b")
    for path in py_files():
        rel = path.relative_to(REPO)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            m = pattern.match(line)
            if not m:
                continue
            name = m.group(1).lower()
            if any(h in name for h in MONEY_HINTS) and not any(
                x in name for x in MONEY_EXCLUDE
            ):
                findings.append(Finding(
                    "money-float", str(rel), i,
                    f"字段 {m.group(1)} 用了 float；金额必须 Money/Decimal（硬边界 2）",
                ))


def check_confidence_float(findings: list[Finding]) -> None:
    """硬边界 3：不存模型产出的置信度数值。"""
    pattern = re.compile(r"^\s*(confidence\w*|\w*_confidence)\s*:\s*(float|Decimal)")
    for path in py_files():
        rel = path.relative_to(REPO)
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.match(line):
                findings.append(Finding(
                    "confidence-float", str(rel), i,
                    "置信度不得存为数值；用 EvidenceLevel + derive_confidence（硬边界 3）",
                ))


def check_events_registered(findings: list[Finding]) -> None:
    """域 events.py 引用的事件必须在 catalog 里存在。"""
    catalog_path = REPO / "shared/events/catalog.py"
    if not catalog_path.exists():
        findings.append(Finding("events", "shared/events/catalog.py", None, "缺失"))
        return
    declared = set(re.findall(
        r"^class (\w+)\(DomainEvent\)", catalog_path.read_text(encoding="utf-8"), re.M
    ))
    for path in REPO.glob("domains/*/events.py"):
        rel = path.relative_to(REPO)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "shared.events.catalog":
                for alias in node.names:
                    if alias.name not in declared:
                        findings.append(Finding(
                            "unknown-event", str(rel), node.lineno,
                            f"{alias.name} 未在 catalog 定义；事件即契约，先去 catalog 声明",
                        ))


def check_tenant_scoping(findings: list[Finding]) -> None:
    """硬边界 8：repository 方法必须带租户维度。

    两条合法路径：签名里显式有 tenant_id，或者收一个自带 tenant_id 的
    实体参数（add(entity) 这类）。判定实体的方式是看类型注解是不是裸的
    首字母大写名——``ContactPoint`` 算，``str`` / ``TenantId`` / ``int`` 不算。
    """
    primitive_like = {
        "str", "int", "bool", "float", "bytes", "dict", "list", "datetime", "date",
    }

    def takes_entity(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
        for arg in node.args.args + node.args.kwonlyargs:
            if arg.arg == "self" or arg.annotation is None:
                continue
            ann = arg.annotation
            if isinstance(ann, ast.Name):
                name = ann.id
                # 实体类型：首字母大写，且不是 XxxId 这种强类型标量
                if (
                    name[:1].isupper()
                    and name not in primitive_like
                    and not name.endswith("Id")
                ):
                    return True
        return False

    for path in REPO.glob("domains/*/repository.py"):
        rel = path.relative_to(REPO)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            args = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
            if "tenant_id" in args or takes_entity(node):
                continue
            findings.append(Finding(
                "missing-tenant", str(rel), node.lineno,
                f"{node.name}() 既无 tenant_id 参数也不收带租户的实体；"
                f"所有查询强制租户过滤（硬边界 8）",
            ))


def check_agents_coverage(findings: list[Finding]) -> None:
    """AGENTS.md 就近生效：关键目录必须有。"""
    required = list(AGENTS_REQUIRED)
    required += [
        f"domains/{d.name}" for d in (REPO / "domains").iterdir()
        if d.is_dir() and not d.name.startswith(("_", "."))
    ]
    for d in required:
        if not (REPO / d / "AGENTS.md").exists():
            findings.append(Finding("no-agents-md", d, None, "关键目录缺 AGENTS.md"))


def check_domain_structure(findings: list[Finding]) -> None:
    """每个域七件套齐全。"""
    for d in sorted((REPO / "domains").iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        for f in DOMAIN_FILES:
            if not (d / f).exists():
                findings.append(Finding(
                    "domain-structure", f"domains/{d.name}", None, f"缺 {f}",
                ))


def check_stub_purity(findings: list[Finding]) -> None:
    """骨架期专用：函数体只能是 NotImplementedError / ... / pass。

    实现开始后这条会大量报错，届时从 CI 里去掉（或只对未动的模块跑）。
    """
    for path in py_files():
        rel = path.relative_to(REPO)
        if rel.parts[0] in ("tests", "scripts"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for stmt in node.body:
                if isinstance(stmt, (ast.Raise, ast.Pass, ast.Expr)):
                    continue
                findings.append(Finding(
                    "stub-purity", str(rel), stmt.lineno,
                    f"{node.name}() 含实现代码；骨架期函数体应为 NotImplementedError",
                ))
                break


CHECKS = [
    ("分层与依赖方向", check_layering),
    ("金额 float", check_money_float),
    ("置信度数值", check_confidence_float),
    ("事件注册", check_events_registered),
    ("租户过滤", check_tenant_scoping),
    ("AGENTS.md 覆盖", check_agents_coverage),
    ("域结构完整", check_domain_structure),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeOS 结构边界自检")
    parser.add_argument("--skeleton", action="store_true", help="追加骨架期 stub 纯度检查")
    parser.add_argument("--quiet", action="store_true", help="只输出违规")
    args = parser.parse_args()

    checks = list(CHECKS)
    if args.skeleton:
        checks.append(("stub 纯度（骨架期）", check_stub_purity))

    total: list[Finding] = []
    for label, fn in checks:
        findings: list[Finding] = []
        fn(findings)
        total.extend(findings)
        if not args.quiet:
            mark = "✗" if findings else "✓"
            print(f"{mark} {label}" + (f"  ({len(findings)} 处)" if findings else ""))
        if findings:
            for f in findings:
                print(str(f))

    if not args.quiet:
        print()
    if total:
        print(f"发现 {len(total)} 处结构违规。修掉再提交——"
              f"架构腐化是复利的，拖越久越贵。")
        return 1
    if not args.quiet:
        print("结构自检通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
