"""按租户、员工和用户隔离的私有 OAuth 状态；不存入模型可读资料。"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from domains.conversations.gmail_connection import (
    GmailActor,
    GmailTestCommand,
    GmailTestResult,
)
from infra.pilot.config import checked_directory, private_read, private_write
from shared.errors import PermissionDenied
from shared.schemas.identifiers import new_id
from tool_gateway.handlers.mailbox_test import BODY, SUBJECT, MailTestGrant


class PendingGmail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    actor: GmailActor
    operation_id: str
    email: str
    session_digest: str = Field(repr=False)
    state: SecretStr = Field(repr=False)
    verifier: SecretStr = Field(repr=False)
    expires_at: datetime
    consumed: bool = False


class GmailConnectBudget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    actor: GmailActor
    operations: dict[str, datetime] = Field(default_factory=dict, max_length=10)


class GmailBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    actor: GmailActor
    email: str
    mailbox_id: str
    operation_id: str = Field(pattern=r"^gco_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    connected_at: datetime



class GmailTestRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    actor: GmailActor
    binding_operation_id: str
    request_id: UUID
    grant: MailTestGrant
    status: str = "pending"
    tool_call_id: str | None = None
    provider_ref: str | None = None

    def view(self) -> GmailTestResult:
        return GmailTestResult(
            request_id=self.request_id, sender=self.grant.sender, recipient=self.grant.recipient,
            subject=SUBJECT, body=BODY, status=self.status, tool_call_id=self.tool_call_id,
            provider_ref=self.provider_ref, created_at=self.grant.approved_at,
        )

class GmailWebStore:
    """本进程私有文件只在显式配置目录；跨进程 flock 防重放和并发覆盖。"""

    def __init__(self, root: Path):
        checked_directory(root)
        self.root = root

    @staticmethod
    def key(actor: GmailActor) -> str:
        return hashlib.sha256(
            f"{actor.tenant_id}\0{actor.employee_id}\0{actor.user_id}".encode()
        ).hexdigest()

    @contextmanager
    def lock(self, actor: GmailActor) -> Iterator[None]:
        checked_directory(self.root)
        fd = os.open(self.root / (self.key(actor) + ".lock"),
                     os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise PermissionDenied("邮箱连接状态不可用")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _pending(self, actor: GmailActor) -> Path:
        return self.root / (self.key(actor) + ".pending.json")

    @staticmethod
    def _encode(pending: PendingGmail) -> bytes:
        value = pending.model_dump(mode="json")
        value["state"] = pending.state.get_secret_value()
        value["verifier"] = pending.verifier.get_secret_value()
        return json.dumps(value).encode()

    def _budget(self, actor: GmailActor) -> GmailConnectBudget:
        path = self.root / (self.key(actor) + ".connect-budget.json")
        budget = (GmailConnectBudget.model_validate_json(private_read(path))
                  if path.exists() else GmailConnectBudget(actor=actor))
        if budget.actor != actor:
            raise PermissionDenied("邮箱授权限额归属无效")
        return budget

    def connection_allowed(self, actor: GmailActor, operation_id: str, now: datetime) -> bool:
        with self.lock(actor):
            started = self._budget(actor).operations.get(operation_id)
            return started is not None and started <= now < started + timedelta(minutes=10)

    def begin(self, actor: GmailActor, session: str, email: str,
              now: datetime) -> PendingGmail:
        with self.lock(actor):
            current = self.binding(actor)
            if current is not None and current.email != email:
                raise PermissionDenied("请使用已绑定的 Gmail 地址重新授权")
            budget = self._budget(actor)
            operations = {key: value for key, value in budget.operations.items()
                          if now - timedelta(minutes=10) < value <= now}
            if len(operations) >= 10:
                raise PermissionDenied("邮箱授权请求过于频繁，请十分钟后重试")
            pending = PendingGmail(
                actor=actor, operation_id=new_id("gco"), email=email,
                session_digest=hashlib.sha256(session.encode()).hexdigest(),
                state=SecretStr(secrets.token_urlsafe(32)),
                verifier=SecretStr(secrets.token_urlsafe(64)),
                expires_at=now + timedelta(minutes=10),
            )
            operations[pending.operation_id] = now
            private_write(self.root / (self.key(actor) + ".connect-budget.json"),
                          GmailConnectBudget(actor=actor, operations=operations).model_dump_json().encode())
            private_write(self._pending(actor), self._encode(pending))
            return pending

    def claim(self, actor: GmailActor, session: str, state: str,
              now: datetime) -> PendingGmail:
        with self.lock(actor):
            try:
                pending = PendingGmail.model_validate_json(private_read(self._pending(actor)))
                if (pending.actor != actor or pending.consumed or pending.expires_at <= now
                        or not hmac.compare_digest(pending.state.get_secret_value(), state)
                        or not hmac.compare_digest(pending.session_digest,
                                                   hashlib.sha256(session.encode()).hexdigest())):
                    raise ValueError()
            except Exception:  # noqa: BLE001 - 私有授权边界只返回固定安全错误
                raise PermissionDenied("授权已过期、已使用或不属于当前登录会话") from None
            claimed = pending.model_copy(update={"consumed": True})
            private_write(self._pending(actor), self._encode(claimed))
            return claimed

    def credentials_file(self, operation_id: str) -> Path:
        import re
        if re.fullmatch(r"gco_[0-7][0-9A-HJKMNP-TV-Z]{25}", operation_id) is None:
            raise PermissionDenied("邮箱绑定无效")
        return self.root / (operation_id + ".credentials.json")

    def binding(self, actor: GmailActor) -> GmailBinding | None:
        path = self.root / (self.key(actor) + ".binding.json")
        if not path.exists():
            return None
        result = GmailBinding.model_validate_json(private_read(path))
        if result.actor != actor:
            raise PermissionDenied("邮箱归属不匹配")
        return result

    def publish(self, pending: PendingGmail, mailbox_id: str, now: datetime) -> GmailBinding:
        with self.lock(pending.actor):
            current = PendingGmail.model_validate_json(private_read(self._pending(pending.actor)))
            if current != pending or not current.consumed or current.expires_at <= now:
                raise PermissionDenied("授权已被新的连接请求替代")
            # Connector 已验证并私密保存凭证；这里只检查文件是否符合存储边界。
            private_read(self.credentials_file(pending.operation_id))
            binding = GmailBinding(actor=pending.actor, email=pending.email,
                                   mailbox_id=mailbox_id, operation_id=pending.operation_id,
                                   connected_at=now)
            private_write(self.root / (self.key(pending.actor) + ".binding.json"),
                          binding.model_dump_json().encode())
            self._pending(pending.actor).unlink()
            return binding

    def bindings(self, tenant_id: str) -> list[GmailBinding]:
        result = []
        for path in self.root.glob("*.binding.json"):
            binding = GmailBinding.model_validate_json(private_read(path))
            if binding.actor.tenant_id == tenant_id and path.name == self.key(binding.actor) + ".binding.json":
                result.append(binding)
        return result

    def _test_path(self, actor: GmailActor, request_id: UUID) -> Path:
        return self.root / (self.key(actor) + ".test-" + str(request_id) + ".json")

    def test_record(self, actor: GmailActor, request_id: UUID) -> GmailTestRecord | None:
        path = self._test_path(actor, request_id)
        if not path.exists():
            return None
        record = GmailTestRecord.model_validate_json(private_read(path))
        if record.actor != actor or record.request_id != request_id:
            raise PermissionDenied("邮箱测试归属无效")
        return record

    def latest_test(self, actor: GmailActor) -> GmailTestRecord | None:
        path = self.root / (self.key(actor) + ".latest-test.json")
        if not path.exists():
            return None
        request_id = UUID(json.loads(private_read(path))["request_id"])
        return self.test_record(actor, request_id)

    def prepare_test(self, actor: GmailActor, binding: GmailBinding,
                     command: GmailTestCommand, now: datetime) -> GmailTestRecord:
        if binding.actor != actor:
            raise PermissionDenied("邮箱测试归属无效")
        with self.lock(actor):
            existing = self.test_record(actor, command.request_id)
            if existing is not None:
                if (existing.grant.recipient != command.email
                        or existing.binding_operation_id != binding.operation_id):
                    raise PermissionDenied("原测试请求不能修改收发地址或授权")
                return existing
            last = self.latest_test(actor)
            if last is not None and last.status not in {"succeeded", "duplicate", "rejected"}:
                raise PermissionDenied("上次测试结果尚未核实，请按原请求核对，勿创建新发送")
            count = 0
            for path in self.root.glob(self.key(actor) + ".test-*.json"):
                record = GmailTestRecord.model_validate_json(private_read(path))
                if record.actor != actor:
                    raise PermissionDenied("邮箱测试归属无效")
                if record.grant.approved_at.date() == now.date():
                    count += 1
            if count >= 5:
                raise PermissionDenied("今日邮箱诊断次数已用完")
            grant = MailTestGrant(new_id("mtg"), actor.tenant_id, actor.employee_id,
                                  binding.email, command.email, now, now + timedelta(minutes=45))
            record = GmailTestRecord(actor=actor, binding_operation_id=binding.operation_id,
                                     request_id=command.request_id, grant=grant)
            private_write(self._test_path(actor, command.request_id), record.model_dump_json().encode())
            private_write(self.root / (self.key(actor) + ".latest-test.json"),
                          json.dumps({"request_id": str(command.request_id)}).encode())
            return record

    def finish_test(self, record: GmailTestRecord, status: str,
                    tool_call_id: str | None, provider_ref: str | None) -> GmailTestRecord:
        with self.lock(record.actor):
            current = self.test_record(record.actor, record.request_id)
            if current is None or current.grant != record.grant:
                raise PermissionDenied("邮箱测试记录失效")
            if current.status in {"succeeded", "duplicate"}:
                return current
            updated = record.model_copy(update={
                "status": status, "tool_call_id": tool_call_id, "provider_ref": provider_ref,
            })
            private_write(self._test_path(record.actor, record.request_id),
                          updated.model_dump_json().encode())
            return updated
