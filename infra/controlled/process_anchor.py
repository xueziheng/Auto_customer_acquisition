"""启动前建立进程组归属锚点；不承载业务配置或继承业务监听FD。"""

from __future__ import annotations

import os
import select
import signal
import socket
import subprocess
import sys
import time


def anchor(control_fd: int, ready_fd: int) -> int:
    """握手确认后才允许exec；TERM期间持续保留不可复用的组成员身份。"""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    with socket.socket(fileno=control_fd) as control:
        control.sendall(str(os.getpid()).encode("ascii"))
        if control.recv(1) == b"R":
            os.write(ready_fd, b"R")
            os.close(ready_fd)
            if control.recv(1) == b"Q":
                return 0
        # 父监督器失联时锚点仍属于原组；先TERM，最多两秒后释放整个原组。
        os.killpg(os.getpgrp(), signal.SIGTERM)
        time.sleep(2)
        os.killpg(os.getpgrp(), signal.SIGKILL)
    return 2


def bootstrap(control_fd: int, command: list[str]) -> int:
    ready_read, ready_write = os.pipe()
    subprocess.Popen(
        [sys.executable, __file__, "anchor", str(control_fd), str(ready_write)],
        env={"PATH": os.defpath, "PYTHON_DOTENV_DISABLED": "1"},
        pass_fds=(control_fd, ready_write),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(ready_write)
    os.close(control_fd)
    if not select.select([ready_read], [], [], 5)[0] or os.read(ready_read, 1) != b"R":
        return 2
    os.close(ready_read)
    # exec保留业务PID与退出码；仅原调用显式传入的FD继续交给业务进程。
    os.execvpe(command[0], command, os.environ)
    return 2


def main() -> int:
    try:
        if sys.argv[1] == "anchor":
            return anchor(int(sys.argv[2]), int(sys.argv[3]))
        return bootstrap(int(sys.argv[2]), sys.argv[3:])
    except BaseException:  # noqa: BLE001 纯生命周期进程也不得回显raw异常
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
