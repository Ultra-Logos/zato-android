#!/usr/bin/env python3
"""Dependency-free parallel HTTP(S) downloader.

update.renpy.org throttles each connection to roughly 10-30 KB/s, so a plain
single-stream download of the 160 MB Ren'Py SDK can take hours. This script
splits the file into ``--connections`` byte ranges and downloads them in
parallel, then concatenates them in order.

Falls back to a single stream when the server does not advertise
``Accept-Ranges: bytes`` or does not send ``Content-Length``.

Proxy settings are picked up from the usual environment variables
(``http_proxy``, ``https_proxy``, ``all_proxy``) by urllib.
"""

import argparse
import os
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

CHUNK = 1 << 20  # 1 MiB per read
UA = "zato-android-build-fetch/1.0"


def _open(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=60)


def probe(url):
    """Return (length, accept_ranges) or (None, False) when unknown."""
    try:
        with _open(url, {"Range": "bytes=0-0"}) as r:
            length = r.headers.get("Content-Range", "")
            total = int(length.split("/")[-1]) if "/" in length else None
            if total is None:
                cl = r.headers.get("Content-Length")
                total = int(cl) if cl and r.status == 200 else None
            # Some nginx mirrors honour Range requests but do not advertise
            # Accept-Ranges; a 206 response proves range support.
            accept = r.status == 206 or r.headers.get("Accept-Ranges", "").lower() == "bytes"
            return total, accept
    except Exception as exc:  # noqa: BLE001 - best effort probe
        print(f"  probe failed ({exc}); using single stream", file=sys.stderr)
        return None, False


def _fetch_range(url, start, end, path, attempt=0):
    """Download [start, end] into path."""
    try:
        with _open(url, {"Range": f"bytes={start}-{end}"}) as r:
            if r.status != 206:
                raise RuntimeError(f"server ignored range request (HTTP {r.status})")
            with open(path, "wb") as f:
                shutil_copy(r, f)
        expected = end - start + 1
        got = os.path.getsize(path)
        if got != expected:
            raise RuntimeError(f"short read: {got}/{expected} bytes")
    except Exception as exc:  # noqa: BLE001
        if attempt >= 3:
            raise
        wait = 2 ** attempt
        print(f"  retry range {start}-{end} in {wait}s ({exc})", file=sys.stderr)
        time.sleep(wait)
        _fetch_range(url, start, end, path, attempt + 1)


def shutil_copy(src, dst):
    while True:
        buf = src.read(CHUNK)
        if not buf:
            return
        dst.write(buf)


def download(url, out, connections=16):
    total, accept = probe(url)
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)

    if not total or not accept:
        print(f"downloading {url} (single stream)")
        tmp = out + ".part"
        with _open(url) as r, open(tmp, "wb") as f:
            shutil_copy(r, f)
        os.replace(tmp, out)
        print(f"  -> {out} ({os.path.getsize(out):,} bytes)")
        return

    part_dir = out + ".parts"
    os.makedirs(part_dir, exist_ok=True)
    size = (total + connections - 1) // connections
    ranges = []
    for i in range(connections):
        start = i * size
        if start >= total:
            break
        end = min(start + size - 1, total - 1)
        ranges.append((i, start, end))

    print(f"downloading {url}")
    print(f"  {total:,} bytes in {len(ranges)} parallel ranges")
    done = 0
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        futures = {
            pool.submit(_fetch_range, url, s, e, os.path.join(part_dir, f"part-{i:03d}")): (i, s, e)
            for i, s, e in ranges
        }
        for fut in as_completed(futures):
            fut.result()
            with lock:
                done += 1
                pct = 100 * done // len(ranges)
                print(f"  {done}/{len(ranges)} ranges ({pct}%)")

    tmp = out + ".part"
    with open(tmp, "wb") as dst:
        for i, _s, _e in ranges:
            with open(os.path.join(part_dir, f"part-{i:03d}"), "rb") as src:
                shutil_copy(src, dst)

    got = os.path.getsize(tmp)
    if got != total:
        sys.exit(f"download size mismatch: {got} != {total}")
    os.replace(tmp, out)
    for i, _s, _e in ranges:
        os.unlink(os.path.join(part_dir, f"part-{i:03d}"))
    os.rmdir(part_dir)
    print(f"  -> {out} ({total:,} bytes)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument(
        "--connections",
        type=int,
        default=int(os.environ.get("FETCH_CONNECTIONS", "16")),
        help="number of parallel range requests (default 16, env FETCH_CONNECTIONS)",
    )
    args = ap.parse_args()
    if os.path.exists(args.out) and os.path.getsize(args.out) > 0:
        print(f"{args.out} already exists, skipping")
        return
    download(args.url, args.out, max(1, args.connections))


if __name__ == "__main__":
    main()
