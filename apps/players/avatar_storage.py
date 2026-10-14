"""Shared private storage adapter. Uses configured AWS SDK credentials, never browser URLs."""

from typing import Protocol

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from django.conf import settings


class AvatarStorage(Protocol):
    def put(self, key: str, content: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...


class S3AvatarStorage:
    def __init__(self) -> None:
        self.bucket = settings.AVATAR_S3_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.AVATAR_S3_ENDPOINT_URL or None,
            region_name=settings.AVATAR_S3_REGION,
            config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
        )

    def put(self, key: str, content: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType="image/jpeg",
            ServerSideEncryption="AES256",
        )

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            content = body.read(settings.AVATAR_CACHE_MAX_BYTES + 1)
            if len(content) > settings.AVATAR_CACHE_MAX_BYTES:
                raise ValueError("storage_size")
            return bytes(content)
        finally:
            body.close()

    def delete(self, key: str) -> None:
        if key:
            self.client.delete_object(Bucket=self.bucket, Key=key)


def get_storage() -> AvatarStorage:
    return S3AvatarStorage()
