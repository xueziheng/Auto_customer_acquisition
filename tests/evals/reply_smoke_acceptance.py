"""三次真实模型烟测；只用合成 Gmail，不代替冻结语料或真实客户验收。

从仓库根目录运行 ``python -m tests.evals.reply_smoke_acceptance --live
--settings-file ABSOLUTE_PRIVATE_JSON --max-calls 3 --report NEW_PRIVATE_JSON``。
凭证仅由可信环境解析器读取；报告路径不可复用，不提供自动重试或恢复付费调用。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from infra.pilot.config import checked_directory, private_write
from infra.secrets import EnvironmentSecretResolver
from infra.standalone.settings import StandaloneModelSettings, load_model_settings
from tests.evals.reply_live_acceptance import (
    invocation_ledger,
    positive_chain,
    unsubscribe_chain,
)
from tests.integration.test_pilot_persistence import initialized, owned_profiles
from tests.integration.test_reply_completion import configure_playbook
from tests.integration.test_standalone_reply_chain import (
    locked,
    probe,
    standalone_reply_chain,
)


@dataclass(frozen=True)
class SmokeSettings:
    """秘密引用仍封装在原配置中；解析器及其环境不得进入 repr 或报告。"""

    settings: StandaloneModelSettings
    resolver: EnvironmentSecretResolver = field(repr=False)


def load_smoke_settings(
    path: Path, maximum: int, environ: Mapping[str, str]
) -> SmokeSettings:
    """资源创建前校验许可，且只在内存收紧本次合成租户的额度。"""
    try:
        if not path.is_absolute() or type(maximum) is not int or maximum != 3:
            raise ValueError
        config = load_model_settings(path)
        if (
            not config.reply_enabled
            or not config.model_data_export_enabled
            or config.research is not None
            or min(config.limits.tenant_calls, config.limits.employee_calls) < 3
        ):
            raise ValueError
        resolver = EnvironmentSecretResolver(environ)
        if not resolver.resolve(config.secret_ref).strip():
            raise ValueError
        bounded = config.model_copy(
            update={
                "limits": config.limits.model_copy(
                    update={
                        "tenant_calls": 3,
                        "employee_calls": 3,
                        "tenant_concurrency": 1,
                        "employee_concurrency": 1,
                    }
                )
            }
        )
        return SmokeSettings(bounded, resolver)
    except Exception:  # noqa: BLE001 不回显配置、秘密引用或解析器异常
        raise ValueError("烟测配置或三次调用额度无效") from None


def save_report(path: Path, report: dict[str, Any]) -> None:
    """沿原私有文件写入器原子保存安全报告，不序列化配置或异常原文。"""
    private_write(
        path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode()
    )


def claim_report(path: Path) -> dict[str, Any]:
    """抢占全新报告路径；已有结果或并发运行不能触发新的模型请求。"""
    try:
        if not path.is_absolute():
            raise ValueError
        checked_directory(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
    except Exception:  # noqa: BLE001 不回显私有路径
        raise ValueError("烟测需要私有目录中的全新绝对报告路径") from None
    report: dict[str, Any] = {
        "scope": "synthetic_reply_smoke",
        "real_model": True,
        "gmail": "controlled",
        "customer_mail_sent": False,
        "corpus": "not_run",
        "max_calls": 3,
        "steps": {"probe": "not_run", "positive": "not_run", "unsubscribe": "not_run"},
        "checkpoints": [],
        "ledger": None,
        "error": None,
        "smoke_complete": False,
        "cleanup": "not_run",
    }
    save_report(path, report)
    return report


async def run_smoke(
    chain: dict[str, Any], report: dict[str, Any], report_path: Path
) -> dict[str, Any]:
    """每步保存实际账本；失败或未知即结束，不改变规范 Run 或重发请求。"""
    report.update(
        tenant_id=str(chain["route"].tenant_id),
        model=chain["model"].model,
        configuration_version=chain["model"].configuration_version,
        real_model=chain["model_provider"] is None,
    )

    async def snapshot() -> dict[str, Any]:
        ledger = await invocation_ledger(chain)
        report["ledger"] = ledger
        save_report(report_path, report)
        return ledger

    async def exercise(worker: Any) -> None:
        if not report["real_model"] or (await snapshot())["calls"] != 0:
            raise ValueError("烟测需要全新合成租户及真实模型端口")
        await configure_playbook(chain, worker)
        for expected, (name, action) in enumerate(
            (
                ("probe", probe),
                ("positive", positive_chain),
                ("unsubscribe", unsubscribe_chain),
            ),
            start=1,
        ):
            report["steps"][name] = "running"
            save_report(report_path, report)
            failure: str | None = None
            try:
                await action(chain, worker)
            except Exception as error:  # noqa: BLE001 失败也必须保存付费账本
                failure = type(error).__name__
            ledger = await snapshot()
            valid = ledger["calls"] == expected and all(
                row["state"] == "succeeded" for row in ledger["records"]
            )
            report["steps"][name] = failure or (
                "passed" if valid else "ledger_incomplete"
            )
            report["checkpoints"].append({"step": name, "ledger": ledger})
            save_report(report_path, report)
            if failure is not None or not valid:
                return

    try:
        await locked(chain, exercise)
    except (asyncio.CancelledError, KeyboardInterrupt):
        report["error"] = "cancelled"
        report["smoke_complete"] = False
        raise
    except Exception as error:  # noqa: BLE001 不记录可能携带输入的底层异常原文
        report["error"] = type(error).__name__
    finally:
        try:
            await snapshot()
        except (asyncio.CancelledError, KeyboardInterrupt):
            report["error"] = "cancelled"
        except Exception:  # noqa: BLE001 已有检查点保持原样，不将未知用量补零
            report["error"] = "ledger_unavailable"
        report["smoke_complete"] = bool(
            report["real_model"]
            and report["error"] is None
            and all(value == "passed" for value in report["steps"].values())
            and report["ledger"] is not None
            and report["ledger"]["calls"] == 3
            and all(row["state"] == "succeeded" for row in report["ledger"]["records"])
        )
        save_report(report_path, report)
    return report


async def run_owned(
    config: SmokeSettings, report: dict[str, Any], report_path: Path
) -> None:
    """只管理独立 owned 存储；失败只停容器，完整成功证据落盘后才删卷。"""
    directory = Path(tempfile.mkdtemp(prefix="reply-smoke-", dir=report_path.parent))
    report["evidence_directory"] = str(directory)
    save_report(report_path, report)
    resources = owned_profiles.__wrapped__(directory)
    owned = next(resources)
    allow_destroy = False
    try:
        # migrate 内部用 asyncio.run；必须离开当前循环执行。取消不等于线程已停止。
        initialization = asyncio.create_task(asyncio.to_thread(initialized, *owned))
        try:
            storage = (await asyncio.shield(initialization)).config
        except asyncio.CancelledError:
            # 等到创建线程退出再清理，避免它在清理之后继续创建新的 owned 容器。
            while not initialization.done():
                try:
                    await asyncio.shield(initialization)
                except asyncio.CancelledError:
                    continue
                except Exception:  # noqa: BLE001 原取消仍是终态，线程故障只需完成回收
                    break
            if not initialization.cancelled():
                initialization.exception()
            raise
        async with standalone_reply_chain(
            storage,
            directory,
            model=config.settings,
            model_resolver=config.resolver,
            model_provider=None,
        ) as chain:
            await run_smoke(chain, report, report_path)
        # context 关闭也可能失败/取消；直到它完成且成功账本已落盘才可销毁。
        if report["smoke_complete"] and report["error"] is None:
            save_report(report_path, report)
            allow_destroy = True
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:
        report["error"] = (
            "cancelled"
            if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt))
            else type(error).__name__
        )
        report["smoke_complete"] = False
        raise
    finally:
        if allow_destroy:
            try:
                next(resources, None)
                shutil.rmtree(directory)
                report["cleanup"] = "passed"
            except Exception:  # noqa: BLE001 保留剩余 owner 目录和已落盘完整账本
                report["cleanup"] = "failed"
                report["smoke_complete"] = False
        else:
            report["smoke_complete"] = False
            report["cleanup"] = "retained_for_review"
            for profile in reversed(owned[1]):
                try:
                    profile.stop()
                except Exception:  # noqa: BLE001 不沿 owner 校验失败继续删除资源
                    report["cleanup"] = "retained_stop_failed"
                finally:
                    profile.client.close()
            # 原 fixture 的删除逻辑在 yield 后，仅成功路径推进 next；关闭不推进删除。
            resources.close()
        save_report(report_path, report)


class SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ValueError("烟测参数无效")


def main(argv: Sequence[str] | None = None) -> int:
    """默认零网络；显式 live、私有配置和三次额度全部满足才启动。"""
    report: dict[str, Any] | None = None
    report_path: Path | None = None
    try:
        parser = SafeParser(description="三次合成回复真实模型烟测", allow_abbrev=False)
        parser.add_argument("--live", action="store_true")
        parser.add_argument("--settings-file", type=Path)
        parser.add_argument("--max-calls", type=int)
        parser.add_argument("--report", type=Path)
        args = parser.parse_args(argv)
        if not args.live:
            print(json.dumps({"status": "not_run", "reason": "explicit_live_required"}))
            return 0
        if args.settings_file is None or args.report is None:
            raise ValueError("烟测参数缺失")
        config = load_smoke_settings(args.settings_file, args.max_calls, os.environ)
        report_path = args.report
        report = claim_report(report_path)
        # 原辅助代码可能输出迁移/容器诊断；仅安全报告可以进入调用者输出。
        with (
            open(os.devnull, "w") as hidden,
            redirect_stdout(hidden),
            redirect_stderr(hidden),
        ):
            asyncio.run(run_owned(config, report, report_path))
    except (Exception, asyncio.CancelledError, KeyboardInterrupt) as error:  # noqa: BLE001 CLI 只输出固定安全结果
        if report is not None and report_path is not None:
            report["error"] = type(error).__name__
            report["smoke_complete"] = False
            try:
                save_report(report_path, report)
            except Exception:  # noqa: BLE001 落盘失败也不得回显底层配置或凭证
                report["error"] = "report_write_failed"
        print(
            json.dumps(
                {
                    "status": "failed" if report else "not_run",
                    "reason": "smoke_incomplete",
                }
            )
        )
        return 1 if report else 2
    passed = bool(report and report["smoke_complete"] and report["cleanup"] == "passed")
    print(json.dumps({"status": "passed" if passed else "failed", "corpus": "not_run"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
