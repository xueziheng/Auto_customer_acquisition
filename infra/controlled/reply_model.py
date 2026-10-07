"""显式受控模型响应和每次调用指纹；不读取原件、凭证或写业务结果。"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

from .config import ControlledError


class ControlledReplyModelClient:
    def __init__(self, path: Path, *, tenant_id: str) -> None:
        self.path, self.tenant_id = path, tenant_id
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS responses (tenant TEXT, fingerprint TEXT, response TEXT, PRIMARY KEY(tenant,fingerprint))"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS calls (tenant TEXT, fingerprint TEXT)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)

    @staticmethod
    def _fingerprint(payload: Mapping[str, object]) -> str:
        if set(payload) != {"subject", "body"} or any(
            not isinstance(v, str) or not v.strip() for v in payload.values()
        ):
            raise ControlledError("controlled_model_input_rejected")
        return hashlib.sha256(
            json.dumps(dict(payload), sort_keys=True).encode()
        ).hexdigest()

    def set_response(self, payload: Mapping[str, object], response: str) -> None:
        """演练操作者仅配置外部模型响应；真实Agent仍执行所有结构/证据护栏。"""
        fingerprint = self._fingerprint(payload)
        if not isinstance(response, str) or not response.strip():
            raise ControlledError("controlled_model_input_rejected")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO responses VALUES (?,?,?)",
                (self.tenant_id, fingerprint, response),
            )

    async def complete_json(
        self,
        *,
        model: str,
        system_prompt: str,
        payload: Mapping[str, object],
        max_output_tokens: int,
    ) -> str:
        fingerprint = self._fingerprint(payload)
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO calls VALUES (?,?)", (self.tenant_id, fingerprint)
            )
            row = connection.execute(
                "SELECT response FROM responses WHERE tenant=? AND fingerprint=?",
                (self.tenant_id, fingerprint),
            ).fetchone()
        if row is None:
            raise ControlledError("controlled_model_response_unconfigured")
        return str(row[0])

    def call_count(self) -> int:
        with closing(self._connect()) as connection, connection:
            return int(
                connection.execute(
                    "SELECT count(*) FROM calls WHERE tenant=?", (self.tenant_id,)
                ).fetchone()[0]
            )
