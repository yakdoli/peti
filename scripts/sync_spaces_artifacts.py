#!/usr/bin/env python3
"""Sync local artifacts to S3-compatible Spaces storage."""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync artifacts to S3-compatible storage")
    parser.add_argument("--source", default="artifacts", help="Local source directory")
    parser.add_argument("--prefix", default="artifacts/", help="Remote key prefix")
    parser.add_argument("--delete", action="store_true", help="Delete remote keys absent locally")
    parser.add_argument("--dry-run", action="store_true", help="Plan only")
    parser.add_argument("--log-every", type=int, default=500, help="Progress interval")
    return parser.parse_args()


def make_client():
    raw_endpoint = os.environ["S3_ENDPOINT"]
    parsed = urlparse(raw_endpoint)
    host_parts = parsed.netloc.split(".")
    if len(host_parts) >= 4 and host_parts[-2:] == ["digitaloceanspaces", "com"]:
        bucket = host_parts[0]
        region = host_parts[1]
        endpoint = f"{parsed.scheme}://{region}.digitaloceanspaces.com"
    else:
        bucket = os.environ["S3_BUCKET"]
        region = os.environ.get("S3_REGION", "us-east-1")
        endpoint = raw_endpoint

    client = boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_ACCESS_SECRET"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    return client, bucket


def iter_local_files(source: Path, prefix: str) -> dict[str, Path]:
    prefix = prefix.strip("/")
    result: dict[str, Path] = {}
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        if path.name.endswith(".tmp"):
            continue
        key = f"{prefix}/{path.relative_to(source).as_posix()}" if prefix else path.relative_to(source).as_posix()
        result[key] = path
    return result


def remote_objects(client, bucket: str, prefix: str) -> dict[str, int]:
    objects: dict[str, int] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/"):
                objects[key] = int(obj.get("Size", 0))
    return objects


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def main() -> None:
    args = parse_args()
    source = Path(args.source)
    if not source.exists():
        raise SystemExit(f"source not found: {source}")

    prefix = args.prefix.strip("/")
    prefix_for_list = f"{prefix}/" if prefix else ""
    client, bucket = make_client()
    local = iter_local_files(source, prefix)
    remote = remote_objects(client, bucket, prefix_for_list)

    to_upload = [(key, path) for key, path in local.items() if remote.get(key) != path.stat().st_size]
    to_delete = [key for key in remote if key not in local] if args.delete else []

    print(
        f"plan bucket={bucket} prefix={prefix_for_list} local={len(local)} "
        f"remote={len(remote)} upload={len(to_upload)} delete={len(to_delete)} dry_run={args.dry_run}",
        flush=True,
    )
    if args.dry_run:
        return

    uploaded = deleted = bytes_uploaded = 0
    for key, path in to_upload:
        extra_args = {
            "ContentType": content_type(path),
            "Metadata": {"sha256": sha256_file(path)},
        }
        client.upload_file(str(path), bucket, key, ExtraArgs=extra_args)
        uploaded += 1
        bytes_uploaded += path.stat().st_size
        if args.log_every and uploaded % args.log_every == 0:
            print(f"upload progress uploaded={uploaded}/{len(to_upload)} bytes_uploaded={bytes_uploaded}", flush=True)

    for key in to_delete:
        client.delete_object(Bucket=bucket, Key=key)
        deleted += 1
        if args.log_every and deleted % args.log_every == 0:
            print(f"delete progress deleted={deleted}/{len(to_delete)}", flush=True)

    print(
        f"complete uploaded={uploaded} deleted={deleted} bytes_uploaded={bytes_uploaded}",
        flush=True,
    )


if __name__ == "__main__":
    main()
