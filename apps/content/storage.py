"""Dedicated banner storage; independent of avatar cache activation and credentials."""

from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from django.conf import settings


def configured() -> bool:
    return bool(settings.BANNER_S3_BUCKET and settings.BANNER_ASSET_ORIGIN.startswith("https://"))


def client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=settings.BANNER_S3_ENDPOINT_URL or None,
        region_name=settings.BANNER_S3_REGION,
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
    )


def put(key: str, content: bytes) -> None:
    client().put_object(
        Bucket=settings.BANNER_S3_BUCKET,
        Key=key,
        Body=content,
        ContentType="image/webp",
        ServerSideEncryption="AES256",
        CacheControl="public, max-age=31536000, immutable",
    )


def get(key: str) -> bytes:
    body = client().get_object(Bucket=settings.BANNER_S3_BUCKET, Key=key)["Body"]
    try:
        content = bytes(body.read(settings.BANNER_MAX_BYTES + 1))
        if len(content) > settings.BANNER_MAX_BYTES:
            raise ValueError("asset_size")
        return content
    finally:
        body.close()
