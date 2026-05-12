#!/usr/bin/env python3
"""Sync local artifacts to S3-compatible Spaces storage."""

from __future__ import annotations

import argparse
import concurrent.futures
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
    parser.add_argument(
        "--files-from",
        default="",
        help="Optional newline-delimited file list to upload without remote diffing.",
    )
    parser.add_argument("--delete", action="store_true", help="Delete remote keys absent locally")
    parser.add_argument("--dry-run", action="store_true", help="Plan only")
    parser.add_argument("--log-every", type=int, default=500, help="Progress interval")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent file uploads")
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


def iter_listed_files(source: Path, prefix: str, files_from: Path) -> dict[str, Path]:
    prefix = prefix.strip("/")
    result: dict[str, Path] = {}
    source = source.resolve()
    for line in files_from.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        path = Path(value)
        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()
        if not path.is_file() or path.name.endswith(".tmp"):
            continue
        try:
            relative = path.relative_to(source)
        except ValueError:
            relative = path.relative_to(Path.cwd().resolve())
        key = f"{prefix}/{relative.as_posix()}" if prefix else relative.as_posix()
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


def upload_one(client, bucket: str, key: str, path: Path) -> int:
    extra_args = {
        "ContentType": content_type(path),
        "Metadata": {"sha256": sha256_file(path)},
    }
    client.upload_file(str(path), bucket, key, ExtraArgs=extra_args)
    return path.stat().st_size


def main() -> None:
    args = parse_args()
    source = Path(args.source)
    if not source.exists():
        raise SystemExit(f"source not found: {source}")

    prefix = args.prefix.strip("/")
    prefix_for_list = f"{prefix}/" if prefix else ""
    client, bucket = make_client()
    files_from = Path(args.files_from) if args.files_from else None
    if files_from:
        if not files_from.exists():
            raise SystemExit(f"files-from not found: {files_from}")
        local = iter_listed_files(source, prefix, files_from)
        remote = {}
        to_upload = list(local.items())
        to_delete = []
    else:
        local = iter_local_files(source, prefix)
        remote = remote_objects(client, bucket, prefix_for_list)
        to_upload = [(key, path) for key, path in local.items() if remote.get(key) != path.stat().st_size]
        to_delete = [key for key in remote if key not in local] if args.delete else []

    print(
        f"plan bucket={bucket} prefix={prefix_for_list} local={len(local)} "
        f"remote={len(remote)} upload={len(to_upload)} delete={len(to_delete)} "
        f"files_from={str(files_from) if files_from else ''} dry_run={args.dry_run}",
        flush=True,
    )
    if args.dry_run:
        return

    uploaded = deleted = bytes_uploaded = 0
    workers = max(1, args.workers)
    if workers == 1:
        for key, path in to_upload:
            bytes_uploaded += upload_one(client, bucket, key, path)
            uploaded += 1
            if args.log_every and uploaded % args.log_every == 0:
                print(f"upload progress uploaded={uploaded}/{len(to_upload)} bytes_uploaded={bytes_uploaded}", flush=True)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            iterator = iter(to_upload)
            max_pending = max(workers * 4, workers)
            pending: set[concurrent.futures.Future[int]] = set()
            for _ in range(min(max_pending, len(to_upload))):
                key, path = next(iterator)
                pending.add(executor.submit(upload_one, client, bucket, key, path))

            while pending:
                done, pending = concurrent.futures.wait(
                    pending,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    bytes_uploaded += future.result()
                    uploaded += 1
                    if args.log_every and uploaded % args.log_every == 0:
                        print(
                            f"upload progress uploaded={uploaded}/{len(to_upload)} bytes_uploaded={bytes_uploaded}",
                            flush=True,
                        )
                    next_item = next(iterator, None)
                    if next_item is not None:
                        key, path = next_item
                        pending.add(executor.submit(upload_one, client, bucket, key, path))

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
