"""监督输出的纯白名单过滤，不导入Docker、数据库或验收运行环境。"""

import re


def safe_output(raw: bytes) -> str:
    """只回传固定诊断/pytest摘要，不暴露Uvicorn和依赖任意日志。"""
    lines = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if (re.fullmatch(
            r"t10_parser_(?:status=unavailable;failure=(?:platform|resource|protocol|runtime|unknown)"
            r"|probe=(?:cpu|as|wall|ipc|unknown);exit=(?:-?[0-9]{1,3}|unknown)"
            r";reason=(?:ok|short|timeout|oversized|unknown))", line)
            or re.fullmatch(r"t10_runtime_stop_exit=-?[0-9]+", line)
            or re.fullmatch(r"t10_relay_(?:saturated|active|exec_active|completed|reload_cost)=[0-9]+", line)
            or re.fullmatch(r"(?:runtime_http_error|fixed_exception_type|t10_fixture_error)=[A-Za-z0-9_.]+", line)
            or re.fullmatch(r"[a-zA-Z0-9_/]+\.py:[0-9]+: in [a-zA-Z0-9_]+", line)
            or re.fullmatch(r"t10_(?:worker_started_cycles|forbidden_gateway_calls)=[0-9]+", line)
            or re.fullmatch(r"t10_unit_diag_(?:(?:requests|failed|responses|selection_start|selection_end|text_length)=[0-9]+|disabled=[01])", line)
            or re.fullmatch(r"t10_unit_probe_(?:initial_response|failed|response|summary_visible|disabled)=[01]", line)
            or re.fullmatch(r"(?:FAILED|ERROR) tests/[a-zA-Z0-9_/:.\[\]-]+", line)
            or re.fullmatch(r"[0-9]+ (?:passed|failed|error|errors)(?:, [0-9]+ (?:passed|failed|error|errors))* in [0-9.]+s", line)):
            lines.append(line)
    return "\n".join(lines)

