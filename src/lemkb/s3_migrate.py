"""Copy the whole vault from the current store (local MinIO) to an AWS S3 bucket.

Target settings come from .env (AWS_S3_BUCKET, AWS_S3_REGION, and credentials via the
standard AWS chain or AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY). Use a bucket dedicated
to this knowledge base: keys are stored at the bucket root, exactly as in MinIO.

    lemkb s3-check      can we reach the bucket and write to it? (writes + deletes one test object)
    lemkb s3-migrate    copy every object, verify, then print the .env lines to switch over

Safe to re-run: objects already present on AWS with the same size (and, for papers, the
same sha256) are skipped. The source is never modified or deleted.
"""
from __future__ import annotations

import logging
import os

from . import db, storage
from .config import settings

log = logging.getLogger("lemkb.s3migrate")


def _target():
    bucket = os.environ.get("AWS_S3_BUCKET")
    region = os.environ.get("AWS_S3_REGION")
    if not bucket or not region:
        raise SystemExit("Set AWS_S3_BUCKET and AWS_S3_REGION in .env first.")
    # AWS_S3_ENDPOINT is only for rehearsing the migration against another S3-compatible store.
    client = storage.make_client(os.environ.get("AWS_S3_ENDPOINT"), os.environ.get("AWS_ACCESS_KEY_ID"),
                                 os.environ.get("AWS_SECRET_ACCESS_KEY"), region)
    return client, bucket


def check() -> dict:
    """Who are we on AWS, and can we read + write the bucket? (writes and deletes one tiny object)"""
    import boto3
    t, bucket = _target()
    who = boto3.client("sts", region_name=os.environ["AWS_S3_REGION"],
                       aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID") or None,
                       aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY") or None).get_caller_identity()
    t.head_bucket(Bucket=bucket)
    probe = "_lemkb_write_test.txt"
    t.put_object(Bucket=bucket, Key=probe, Body=b"ok")
    t.delete_object(Bucket=bucket, Key=probe)
    return {"aws_identity": who["Arn"], "bucket": bucket, "read_write": "ok",
            "objects_already_in_bucket": t.list_objects_v2(Bucket=bucket, MaxKeys=1000).get("KeyCount", 0)}


def migrate() -> dict:
    t, bucket = _target()
    src = storage.client()
    with db.connect() as conn:
        hashes = {r["s3_key"]: r["sha256"] for r in conn.execute(
            "SELECT s3_key, sha256 FROM papers WHERE s3_key IS NOT NULL UNION ALL "
            "SELECT s3_key, sha256 FROM mediaassist_downloads").fetchall()}
    stats = {"copied": 0, "skipped_same": 0, "verified": 0, "bytes": 0}
    for page in src.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket):
        for obj in page.get("Contents", []):
            key = dest = obj["Key"]
            try:
                head = t.head_object(Bucket=bucket, Key=dest)
                if head["ContentLength"] == obj["Size"] and head.get("Metadata", {}).get("sha256", hashes.get(key)) == hashes.get(key):
                    stats["skipped_same"] += 1
                    continue
            except t.exceptions.ClientError:
                pass
            got = src.get_object(Bucket=settings.s3_bucket, Key=key)
            body = got["Body"].read()
            if key in hashes and hashes[key] and storage.sha256(body) != hashes[key]:
                raise RuntimeError(f"source object {key} doesn't match its catalog hash; stopping")
            t.put_object(Bucket=bucket, Key=dest, Body=body, ContentType=got.get("ContentType", "application/octet-stream"),
                         Metadata=got.get("Metadata", {}))  # AWS encrypts at rest by default (SSE-S3)
            if key in hashes and hashes[key]:
                if storage.sha256(t.get_object(Bucket=bucket, Key=dest)["Body"].read()) != hashes[key]:
                    raise RuntimeError(f"AWS copy of {key} doesn't match its hash; stopping")
                stats["verified"] += 1
            stats["copied"] += 1
            stats["bytes"] += len(body)
            if stats["copied"] % 50 == 0:
                log.info("copied %d objects (%.1f MB)", stats["copied"], stats["bytes"] / 1e6)
    src_n = sum(p.get("KeyCount", 0) for p in src.get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket))
    dst_n = sum(p.get("KeyCount", 0) for p in t.get_paginator("list_objects_v2").paginate(Bucket=bucket))
    stats.update(source_objects=src_n, target_objects=dst_n, complete=dst_n >= src_n)
    if stats["complete"] and not os.environ.get("AWS_S3_ENDPOINT"):
        with db.connect() as conn:  # record which bucket now holds each file
            conn.execute("UPDATE papers SET s3_bucket=%s WHERE s3_key IS NOT NULL", (bucket,))
            conn.execute("UPDATE mediaassist_downloads SET s3_bucket=%s", (bucket,))
        stats["switch_env_to"] = {"S3_ENDPOINT": "", "S3_BUCKET": bucket, "S3_REGION": os.environ["AWS_S3_REGION"],
                                  "S3_ACCESS_KEY": "", "S3_SECRET_KEY": ""}
    return stats
