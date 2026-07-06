from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Any, Iterator


def _enabled() -> bool:
    return os.getenv("ALLOW_UNOFFICIAL_SCRAPERS", "false").strip().lower() in {"1", "true", "yes", "on"}


def run_instaloader_profile(profile: str, output_dir: str, comments: bool, max_posts: int | None = None, fast_update: bool = True) -> subprocess.CompletedProcess[str]:
    if not _enabled():
        raise RuntimeError("Instaloader is disabled. Set ALLOW_UNOFFICIAL_SCRAPERS=true to enable unofficial local tools.")

    cmd = [
        "instaloader",
        "--dirname-pattern",
        str(pathlib.Path(output_dir) / "{profile}"),
        "--no-compress-json",
        "--metadata-json",
    ]
    if fast_update:
        cmd.append("--fast-update")
    if comments:
        cmd.append("--comments")
    if max_posts:
        cmd += ["--count", str(max_posts)]

    session_dir = os.getenv("INSTALOADER_SESSION_DIR")
    if session_dir:
        cmd += ["--sessionfile", str(pathlib.Path(session_dir) / "instagram.session")]

    cmd.append(profile)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def iter_instaloader_json(output_dir: str) -> Iterator[dict[str, Any]]:
    root = pathlib.Path(output_dir)
    if not root.exists():
        return
    for path in root.rglob("*.json"):
        if path.name.endswith("_comments.json"):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            data["_json_path"] = str(path)
            yield data
