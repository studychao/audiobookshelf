"""Create only the Audiobookshelf prefix and verify private OSS access."""
from __future__ import annotations

from pathlib import Path
import sys

import oss2


def connect() -> oss2.Bucket:
    bucket_name, access_id, access_secret = Path("/opt/audiobookshelf/secrets/oss-passwd").read_text().strip().split(":", 2)
    return oss2.Bucket(
        oss2.AuthV4(access_id, access_secret),
        "https://oss-us-west-1-internal.aliyuncs.com",
        bucket_name,
        region="us-west-1",
        connect_timeout=10,
    )


def main() -> None:
    bucket = connect()
    for key in ("audiobookshelf/", "audiobookshelf/audiobooks/", "audiobookshelf/podcasts/"):
        if not bucket.object_exists(key):
            bucket.put_object(key, b"")
    result = bucket.list_objects(prefix="audiobookshelf/", max_keys=5)
    if result.status != 200:
        raise RuntimeError("Unable to list the Audiobookshelf prefix")
    print("Private OSS prefix ready; V4 authenticated write and list verified over the internal endpoint")


if __name__ == "__main__":
    try:
        main()
    except oss2.exceptions.OssError as error:
        print(f"OSS request failed: status={error.status}, code={error.code}, request_id={error.request_id}", file=sys.stderr)
        raise SystemExit(1) from None
