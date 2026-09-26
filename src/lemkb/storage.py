"""S3 access. Works against local MinIO (S3_ENDPOINT set) or AWS S3 (S3_ENDPOINT empty).

On AWS, credentials come from S3_ACCESS_KEY / S3_SECRET_KEY if set, otherwise from the
standard AWS chain (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars, AWS_PROFILE,
~/.aws/credentials, or an instance/SSO role), so keys never need to live in this repo.
"""
import hashlib

import boto3
from botocore.config import Config

from .config import settings

_client = None


def make_client(endpoint: str | None, access_key: str | None, secret_key: str | None, region: str | None):
    return boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        region_name=region or None,
        # MinIO needs path-style URLs; AWS prefers virtual-hosted style.
        config=Config(signature_version="s3v4", s3={"addressing_style": "path" if endpoint else "virtual"},
                      retries={"max_attempts": 8, "mode": "standard"}),
    )


def client():
    global _client
    if _client is None:
        _client = make_client(settings.s3_endpoint, settings.s3_access_key, settings.s3_secret_key, settings.s3_region)
    return _client


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(key: str, data: bytes, content_type: str, metadata: dict[str, str] | None = None) -> None:
    client().put_object(
        Bucket=settings.s3_bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        # S3 metadata must be ASCII; values are DOIs/hashes/URLs so this is safe after encoding.
        Metadata={k: v.encode("ascii", "backslashreplace").decode() for k, v in (metadata or {}).items()},
    )


def presigned_url(key: str, expires: int = 3600) -> str:
    return client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=expires
    )
