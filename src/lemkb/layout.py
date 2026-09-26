"""The fixed S3 layout, and a one-time migration of older keys into it.

    papers/<flat-doi>.pdf|.xml      original paper files        (key in papers.s3_key)
    parsed/<flat-doi>.json          extracted passages           (key in paper_texts.s3_key)
    mediaassist/raw/<type>/<Component>/<type>_<Component>[_<partner/option>]__<hash12>.xlsx
                                    original MediaAssist exports (key in mediaassist_downloads.s3_key)

Every key is stored in Postgres next to its DOI / export, so nothing depends on parsing names.
"""
from __future__ import annotations

from . import db, harvest, storage
from .config import settings
from .doi import s3_key


def _move(s3, old: str, new: str, sha256: str | None) -> None:
    s3.copy_object(Bucket=settings.s3_bucket, Key=new, CopySource={"Bucket": settings.s3_bucket, "Key": old})
    if sha256 and storage.sha256(s3.get_object(Bucket=settings.s3_bucket, Key=new)["Body"].read()) != sha256:
        raise RuntimeError(f"copy of {old} does not match its sha256; stopping (old key left in place)")


def tidy() -> dict:
    s3 = storage.client()
    moved = {"papers": 0, "parsed": 0}
    with db.connect() as conn:
        for p in conn.execute("SELECT doi, s3_key, sha256 FROM papers WHERE s3_key IS NOT NULL").fetchall():
            new = s3_key(p["doi"], p["s3_key"].rsplit(".", 1)[-1])
            if new != p["s3_key"]:
                _move(s3, p["s3_key"], new, p["sha256"])
                conn.execute("UPDATE papers SET s3_key=%s WHERE doi=%s", (new, p["doi"]))
                s3.delete_object(Bucket=settings.s3_bucket, Key=p["s3_key"])
                moved["papers"] += 1
        for t in conn.execute("SELECT doi, s3_key FROM paper_texts WHERE s3_key IS NOT NULL").fetchall():
            new = s3_key(t["doi"], "json").replace("papers/", "parsed/", 1)
            if new != t["s3_key"]:
                _move(s3, t["s3_key"], new, None)
                conn.execute("UPDATE paper_texts SET s3_key=%s WHERE doi=%s", (new, t["doi"]))
                s3.delete_object(Bucket=settings.s3_bucket, Key=t["s3_key"])
                moved["parsed"] += 1
    moved["mediaassist"] = harvest.tidy_raw_keys()["moved"]
    moved["orphans"] = orphans()
    return moved


def orphans() -> list[str]:
    """Objects in the bucket that no database row points to (should be empty)."""
    with db.connect() as conn:
        known = {r["k"] for r in conn.execute(
            """SELECT s3_key AS k FROM papers WHERE s3_key IS NOT NULL
               UNION SELECT s3_key FROM paper_texts WHERE s3_key IS NOT NULL
               UNION SELECT s3_key FROM mediaassist_downloads""").fetchall()}
    keys = [o["Key"] for page in storage.client().get_paginator("list_objects_v2").paginate(Bucket=settings.s3_bucket)
            for o in page.get("Contents", [])]
    return [k for k in keys if k not in known]
