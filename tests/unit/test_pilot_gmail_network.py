"""真实入口审计边界：只对已配置 Gmail 放行固定端点。"""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("enabled", [False, True])
def test_api_gmail_network_matches_configured_capability(enabled):
    code = r'''import socket
import sys
from types import SimpleNamespace
from apps.api import pilot
from infra.controlled.config import ControlledError

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
port = listener.getsockname()[1]
fd = listener.detach()
enabled = sys.argv[1] == "True"
config = SimpleNamespace(gmail=object() if enabled else None,
                         database_port=19091, object_port=19092, api_port=port)
pilot.PilotConfig.read = lambda path: config
pilot.create_pilot_app = lambda *args: object()
sys.argv = ["pilot", "synthetic-profile", str(fd)]
def resolve(host, port, **kwargs):
    assert host in {"gmail.googleapis.com", "oauth2.googleapis.com"}
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.20", port))]
socket.getaddrinfo = resolve
pilot.uvicorn.Config = lambda *args, **kwargs: object()
class Server:
    started = True
    def __init__(self, config):
        pass
    async def serve(self, sockets):
        connection = sockets[0]
        for host in ("gmail.googleapis.com", "oauth2.googleapis.com"):
            if enabled:
                sys.audit("socket.getaddrinfo", host, 443, 0, 0, 0)
                sys.audit("socket.connect", connection, ("203.0.113.20", 443))
            else:
                try:
                    sys.audit("socket.getaddrinfo", host, 443, 0, 0, 0)
                except ControlledError:
                    pass
                else:
                    raise AssertionError("未配置 Gmail 时不能放行")
        try:
            sys.audit("socket.connect", connection, ("203.0.113.21", 443))
        except ControlledError:
            pass
        else:
            raise AssertionError("不能放行未授权目标")
pilot.uvicorn.Server = Server
assert pilot.main() == 0
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(enabled)], capture_output=True, text=True,
        timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
