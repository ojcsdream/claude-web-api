#!/usr/bin/env python3
"""
Download an image URL to a local file and print JSON metadata.

This is intentionally dependency-free so it works in the Termux/proot setup.
It also understands File Browser share pages such as:
  http://127.0.0.1:8080/share/<hash>
and resolves them to:
  /api/public/dl/<hash>/<filename>
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
IMAGE_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
}


def request_bytes(url: str, timeout: int = 20) -> tuple[bytes, dict[str, str], str]:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "User-Agent": USER_AGENT,
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        final_url = resp.geturl()
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return data, headers, final_url


def looks_like_filebrowser_share(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    match = re.match(r"^/share/([^/?#]+)", parsed.path)
    return match.group(1) if match else None


def resolve_filebrowser_share(url: str, timeout: int = 20) -> tuple[str, dict]:
    share_hash = looks_like_filebrowser_share(url)
    if not share_hash:
        return url, {}

    parsed = urllib.parse.urlparse(url)
    origin = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    meta_url = f"{origin}/api/public/share/{urllib.parse.quote(share_hash)}"
    raw, headers, final_url = request_bytes(meta_url, timeout=timeout)

    try:
        meta = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"File Browser share metadata is not JSON: {exc}") from exc

    name = meta.get("name") or ""
    if not isinstance(name, str) or not name:
        raise RuntimeError("File Browser share metadata does not contain a file name")

    download_url = (
        f"{origin}/api/public/dl/"
        f"{urllib.parse.quote(share_hash)}/"
        f"{urllib.parse.quote(name)}"
    )
    meta["_share_meta_url"] = final_url
    meta["_share_meta_content_type"] = headers.get("content-type", "")
    return download_url, meta


def sniff_image_type(data: bytes, content_type: str = "", path_hint: str = "") -> tuple[str, str]:
    lower_type = (content_type or "").split(";", 1)[0].strip().lower()

    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    if data.startswith(b"BM"):
        return "bmp", "image/bmp"

    ext = Path(urllib.parse.urlparse(path_hint).path).suffix.lower()
    if lower_type.startswith("image/"):
        return (ext.lstrip(".") or lower_type.split("/", 1)[1]), lower_type
    if ext in IMAGE_EXTS:
        return ext.lstrip("."), mimetypes.types_map.get(ext, "application/octet-stream")
    return "bin", lower_type or "application/octet-stream"


def image_size(data: bytes, kind: str) -> tuple[int | None, int | None]:
    if kind == "png" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])

    if kind in {"jpg", "jpeg"}:
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in {0xD8, 0xD9}:
                continue
            if i + 2 > len(data):
                break
            segment_len = struct.unpack(">H", data[i : i + 2])[0]
            if segment_len < 2 or i + segment_len > len(data):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height, width = struct.unpack(">HH", data[i + 3 : i + 7])
                return width, height
            i += segment_len

    if kind == "gif" and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])

    if kind == "bmp" and len(data) >= 26:
        width, height = struct.unpack("<ii", data[18:26])
        return width, abs(height)

    if kind == "webp" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8 " and len(data) >= 30:
            width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
            height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
            return width, height
        if chunk == b"VP8L" and len(data) >= 25:
            b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
            width = 1 + (((b1 & 0x3F) << 8) | b0)
            height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return width, height
        if chunk == b"VP8X" and len(data) >= 30:
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
            return width, height

    return None, None


def safe_output_name(url: str, kind: str, fallback_name: str = "") -> str:
    name = fallback_name or Path(urllib.parse.urlparse(url).path).name
    name = urllib.parse.unquote(name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "downloaded_image"

    ext = Path(name).suffix.lower()
    wanted_ext = ".jpg" if kind == "jpeg" else f".{kind}"
    if kind != "bin" and ext not in IMAGE_EXTS:
        name += wanted_ext
    return name


def main() -> int:
    parser = argparse.ArgumentParser(description="Download and identify image binary data.")
    parser.add_argument("url", help="Image URL or File Browser share URL")
    parser.add_argument(
        "--out-dir",
        default=str(Path(tempfile.gettempdir()) / "codex-images"),
        help="Directory for downloaded image files",
    )
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()

    source_url = args.url
    try:
        download_url, share_meta = resolve_filebrowser_share(source_url, timeout=args.timeout)
        data, headers, final_url = request_bytes(download_url, timeout=args.timeout)
        content_type = headers.get("content-type", "")
        kind, mime = sniff_image_type(data, content_type, final_url)
        width, height = image_size(data, kind)

        if kind == "bin" or not mime.startswith("image/"):
            raise RuntimeError(f"downloaded content is not an image: content_type={content_type!r}")

        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        name = safe_output_name(final_url, kind, str(share_meta.get("name") or ""))
        out_path = out_dir / name
        if out_path.exists():
            stem = out_path.stem
            suffix = out_path.suffix
            n = 2
            while (out_dir / f"{stem}-{n}{suffix}").exists():
                n += 1
            out_path = out_dir / f"{stem}-{n}{suffix}"
        out_path.write_bytes(data)

        print(
            json.dumps(
                {
                    "ok": True,
                    "source_url": source_url,
                    "download_url": download_url,
                    "final_url": final_url,
                    "path": str(out_path),
                    "bytes": len(data),
                    "kind": kind,
                    "mime": mime,
                    "width": width,
                    "height": height,
                    "share_meta": share_meta,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:1000]
        error = f"HTTP {exc.code}: {body or exc.reason}"
    except Exception as exc:
        error = str(exc)

    print(
        json.dumps(
            {
                "ok": False,
                "source_url": source_url,
                "error": error,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
