#!/usr/bin/env python3
"""Download S3-compatible Spaces artifacts to the local workspace."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.config import Config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download artifacts from S3-compatible storage")
    parser.add_argument("--prefix", default="artifacts/", help="Remote key prefix to download")
    parser.add_argument("--dest", default=".", help="Local destination root")
    parser.add_argument("--log-every", type=int, default=500, help="Progress interval")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite even when size matches")
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


def main() -> None:
    args = parse_args()
    dest = Path(args.dest)
    prefix = args.prefix
    client, bucket = make_client()

    total = downloaded = skipped = bytes_downloaded = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            size = int(obj.get("Size", 0))
            local_path = dest / key
            total += 1
            if local_path.exists() and local_path.stat().st_size == size and not args.overwrite:
                skipped += 1
            else:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = local_path.with_suffix(local_path.suffix + ".tmp")
                client.download_file(bucket, key, str(tmp_path))
                tmp_path.replace(local_path)
                downloaded += 1
                bytes_downloaded += size

            if args.log_every and total % args.log_every == 0:
                print(
                    f"progress total={total} downloaded={downloaded} "
                    f"skipped={skipped} bytes_downloaded={bytes_downloaded}",
                    flush=True,
                )

    print(
        f"complete total={total} downloaded={downloaded} "
        f"skipped={skipped} bytes_downloaded={bytes_downloaded}",
        flush=True,
    )


if __name__ == "__main__":
    main()
