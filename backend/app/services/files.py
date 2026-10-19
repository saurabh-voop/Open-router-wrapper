"""S3 object storage for attachments and sandbox outputs (SPEC sections 8.2 and 10).

boto3 is synchronous, so calls run in a worker thread. Objects are written with SSE-KMS
on AWS; MinIO in development uses its default encryption setting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import boto3

from app.core.config import get_settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef


@lru_cache
def s3_client() -> S3Client:
    settings = get_settings()
    kwargs: dict[str, Any] = {"region_name": settings.aws_region}
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    return boto3.client("s3", **kwargs)


def _encryption_args() -> dict[str, str]:
    return {} if get_settings().s3_endpoint_url else {"ServerSideEncryption": "aws:kms"}


async def put_object(key: str, data: bytes, content_type: str) -> None:
    bucket = get_settings().s3_bucket
    await asyncio.to_thread(
        s3_client().put_object,
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        **_encryption_args(),  # type: ignore[arg-type]
    )


async def get_object(key: str) -> bytes:
    bucket = get_settings().s3_bucket

    def _read() -> bytes:
        return s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()

    return await asyncio.to_thread(_read)


async def delete_objects(keys: Sequence[str]) -> None:
    if not keys:
        return
    bucket = get_settings().s3_bucket
    client = s3_client()
    for start in range(0, len(keys), 1000):  # S3 limit per DeleteObjects call
        batch: list[ObjectIdentifierTypeDef] = [{"Key": k} for k in keys[start : start + 1000]]
        await asyncio.to_thread(
            client.delete_objects, Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
        )


async def ensure_bucket() -> None:
    """Development only (S3_ENDPOINT_URL set): create the bucket if the local S3 lacks it."""
    settings = get_settings()
    if not settings.s3_endpoint_url:
        return
    client = s3_client()

    def _ensure() -> None:
        existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
        if settings.s3_bucket not in existing:
            client.create_bucket(
                Bucket=settings.s3_bucket,
                CreateBucketConfiguration={"LocationConstraint": settings.aws_region},  # type: ignore[typeddict-item]
            )

    await asyncio.to_thread(_ensure)
