"""固定私有探针入口，生产parse协议从不接受探针动作。"""

import struct
import sys
import time

from connectors.evidence_text.worker import (
    emit,
    read_control,
    runtime_matches,
    set_resources,
)


def main() -> int:
    """真实CPU信号、有限AS触页、wall与IPC管道回收证明。"""
    try:
        control = read_control()
        if set(control) != {"action", "limits"} or control["action"] not in {
            "cpu",
            "as",
            "wall",
            "ipc",
        }:
            return 70
        limits = control["limits"]
        fields = {
            "cpu_seconds",
            "address_space_bytes",
            "wall_timeout_ms",
            "allocation_chunk_bytes",
            "maximum_probe_bytes",
            "maximum_result_bytes",
            "termination_grace_ms",
        }
        if (
            type(limits) is not dict
            or set(limits) != fields
            or any(type(v) is not int or v <= 0 for v in limits.values())
        ):
            return 70
        set_resources(limits["cpu_seconds"], limits["address_space_bytes"])
        if not runtime_matches():
            return 70
        action = control["action"]
        if action == "cpu":
            while True:
                pass
        if action == "as":
            allocated = []
            used = 0
            try:
                while used < limits["maximum_probe_bytes"]:
                    count = min(
                        limits["allocation_chunk_bytes"],
                        limits["maximum_probe_bytes"] - used,
                    )
                    allocation = bytearray(count)
                    for index in range(0, count, 4096):
                        allocation[index] = 1
                    allocated.append(allocation)
                    used += count
            except MemoryError:
                del allocated
                emit({"probe": "as", "limited": True}, limits["maximum_result_bytes"])
                return 0
            return 72
        if action == "ipc":
            sys.stdout.buffer.write(
                struct.pack("!I", limits["maximum_result_bytes"] + 1)
            )
            sys.stdout.buffer.flush()
        while True:
            time.sleep(1)
    except Exception:  # noqa: BLE001 - 不暴露探针环境
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
