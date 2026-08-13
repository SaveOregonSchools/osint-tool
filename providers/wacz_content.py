from __future__ import annotations

import gzip
import hashlib
import json
import mimetypes
import re
import zipfile
from pathlib import Path
from typing import Any, BinaryIO, Iterator
from urllib.parse import urlparse


MAX_MEDIA_FILE_BYTES = 25 * 1024 * 1024
MAX_MEDIA_TOTAL_BYTES = 500 * 1024 * 1024
IMAGE_EXTENSIONS = {
    "image/avif": ".avif",
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def _warc_records(stream: BinaryIO) -> Iterator[tuple[dict[str, str], bytes]]:
    while True:
        line = stream.readline()
        while line in {b"\r\n", b"\n"}:
            line = stream.readline()
        if not line:
            return
        if not line.startswith(b"WARC/"):
            continue
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if not line:
                return
            if line in {b"\r\n", b"\n"}:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            if ":" in text:
                name, value = text.split(":", 1)
                headers[name.casefold().strip()] = value.strip()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            return
        block = stream.read(max(0, length))
        yield headers, block


def _http_payload(block: bytes) -> tuple[dict[str, str], bytes]:
    separator = b"\r\n\r\n"
    index = block.find(separator)
    separator_length = len(separator)
    if index < 0:
        separator = b"\n\n"
        index = block.find(separator)
        separator_length = len(separator)
    if index < 0:
        return {}, b""
    header_lines = block[:index].decode("iso-8859-1", errors="replace").splitlines()[1:]
    headers: dict[str, str] = {}
    for line in header_lines:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.casefold().strip()] = value.strip()
    return headers, block[index + separator_length :]


def _decode_text(value: bytes) -> str:
    return value.decode("utf-8", errors="replace").strip()


def _text_target_url(target: str) -> str:
    for prefix in ("urn:textFinal:", "urn:text:"):
        if target.startswith(prefix):
            return target[len(prefix) :]
    return ""


def _extension(content_type: str, source_url: str) -> str:
    clean_type = content_type.split(";", 1)[0].casefold().strip()
    if clean_type in IMAGE_EXTENSIONS:
        return IMAGE_EXTENSIONS[clean_type]
    suffix = Path(urlparse(source_url).path).suffix.casefold()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return mimetypes.guess_extension(clean_type) or ".img"


def _page_entries(archive: zipfile.ZipFile) -> dict[str, dict[str, Any]]:
    pages: dict[str, dict[str, Any]] = {}
    for name in archive.namelist():
        if not name.startswith("pages/") or not name.endswith(".jsonl"):
            continue
        with archive.open(name) as handle:
            for raw_line in handle:
                try:
                    item = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                url = str(item.get("url") or "").strip()
                if url:
                    pages[url] = item
    return pages


def extract_wacz_content(wacz_path: Path, output_dir: Path) -> dict[str, Any]:
    """Create an n8n-friendly text and image bundle from a Browsertrix WACZ."""
    output_dir.mkdir(parents=True, exist_ok=True)
    media_dir = output_dir / "media"
    media_dir.mkdir(exist_ok=True)

    final_text: dict[str, str] = {}
    initial_text: dict[str, str] = {}
    media: list[dict[str, Any]] = []
    seen_media: set[str] = set()
    total_media_bytes = 0

    with zipfile.ZipFile(wacz_path) as archive:
        pages = _page_entries(archive)
        warc_names = [
            name
            for name in archive.namelist()
            if name.startswith("archive/") and (name.endswith(".warc") or name.endswith(".warc.gz"))
        ]
        for warc_name in warc_names:
            with archive.open(warc_name) as raw_warc:
                stream: BinaryIO
                if warc_name.endswith(".gz"):
                    stream = gzip.GzipFile(fileobj=raw_warc)
                else:
                    stream = raw_warc
                for headers, block in _warc_records(stream):
                    target = headers.get("warc-target-uri", "")
                    record_type = headers.get("warc-type", "").casefold()
                    text_url = _text_target_url(target)
                    if record_type == "resource" and text_url:
                        text = _decode_text(block)
                        if target.startswith("urn:textFinal:"):
                            final_text[text_url] = text
                        elif text_url not in initial_text:
                            initial_text[text_url] = text
                        continue
                    if record_type != "response" or not target.startswith(("http://", "https://")):
                        continue
                    http_headers, body = _http_payload(block)
                    content_type = http_headers.get("content-type", "").split(";", 1)[0].casefold().strip()
                    if not content_type.startswith("image/") or not body:
                        continue
                    if len(body) > MAX_MEDIA_FILE_BYTES or total_media_bytes + len(body) > MAX_MEDIA_TOTAL_BYTES:
                        continue
                    digest = hashlib.sha256(body).hexdigest()
                    if digest in seen_media:
                        continue
                    seen_media.add(digest)
                    filename = f"{len(media) + 1:04d}-{digest[:12]}{_extension(content_type, target)}"
                    destination = media_dir / filename
                    destination.write_bytes(body)
                    total_media_bytes += len(body)
                    media.append(
                        {
                            "source_url": target,
                            "content_type": content_type,
                            "bytes": len(body),
                            "sha256": digest,
                            "file": f"media/{filename}",
                        }
                    )

    document_urls = set(pages) | set(initial_text) | set(final_text)
    documents: list[dict[str, Any]] = []
    for url in sorted(document_urls):
        page = pages.get(url, {})
        text = final_text.get(url) or str(page.get("text") or "").strip() or initial_text.get(url, "")
        if not text:
            continue
        documents.append(
            {
                "url": url,
                "title": str(page.get("title") or ""),
                "captured_at": str(page.get("ts") or ""),
                "text": text,
            }
        )

    bundle = {
        "format": "social-profile-content-1.0",
        "source_wacz": str(wacz_path.resolve()),
        "documents": documents,
        "media": media,
        "document_count": len(documents),
        "media_count": len(media),
        "notes": [
            "Text is Browsertrix page text, not a guaranteed one-record-per-post export.",
            "Media contains captured image responses and can include profile or interface images as well as post graphics.",
        ],
    }
    content_path = output_dir / "content.json"
    content_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "content_path": str(content_path.resolve()),
        "content_dir": str(output_dir.resolve()),
        "document_count": len(documents),
        "media_count": len(media),
    }
