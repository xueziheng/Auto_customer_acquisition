"""QQ 准备的有界 SDK；只在可被 SIGKILL 的私有子进程内使用。"""
from time import monotonic

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from artifact_store.transport import BlobObjectNotFoundError, BlobReadLimitExceeded
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import _require_key


class ObjectWorker:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline
        self.client = None
        self.bucket = ""

    def call(self, operation: str, *args):
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError()
        if operation == "open" and self.client is None:
            settings = S3ObjectStoreSettings(**args[0])
            self.bucket = settings.bucket
            self.client = boto3.client("s3", endpoint_url=settings.endpoint,
                aws_access_key_id=args[1], aws_secret_access_key=args[2], region_name=settings.region,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                    connect_timeout=remaining, read_timeout=remaining,
                    retries={"total_max_attempts": 1, "mode": "standard"}))
            return None
        if self.client is None:
            raise ValueError("missing preparation client")
        if operation == "close":
            self.client.close()
            return None
        key = _require_key(args[0])
        if operation == "put":
            if not isinstance(args[1], bytes) or not 0 < len(args[1]) <= 16384:
                raise ValueError("invalid preparation object")
            self.client.put_object(Bucket=self.bucket, Key=key, Body=args[1])
        elif operation == "get":
            limit = args[1]
            if type(limit) is not int or not 0 < limit <= 16384:
                raise ValueError("invalid preparation limit")
            try:
                body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                    raise BlobObjectNotFoundError() from None
                raise
            try:
                content = body.read(limit + 1)
                if len(content) > limit:
                    raise BlobReadLimitExceeded()
                return content
            finally:
                body.close()
        elif operation == "delete":
            self.client.delete_object(Bucket=self.bucket, Key=key)
        else:
            raise ValueError("invalid preparation operation")
        return None
