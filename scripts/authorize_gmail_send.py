"""本机 Gmail 发信授权入口；只输出固定状态，不打印令牌。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from connectors.gmail.send_oauth import authorize_local_send


def main() -> int:
    parser = argparse.ArgumentParser(description="授权 Gmail 读取与发送")
    parser.add_argument("--client-file", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--browser", choices=("chrome",), default=None)
    args = parser.parse_args()
    try:
        authorize_local_send(
            args.client_file, args.credentials_file, browser=args.browser
        )
    except Exception:  # noqa: BLE001 - 凭证错误仅输出固定状态
        print(json.dumps({"status": "failed", "reason": "authorization_required"}))
        return 2
    print(json.dumps({"status": "authorized", "scope": "read_and_send"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
