"""Songbook numbers: each prepared song keeps one number for life, like a noraebang book."""
import json
import os
from pathlib import Path

FIRST = 10001

def read(meta_path: Path):
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

def numbers(cache: Path, *additional: Path) -> dict[int, str]:
    """Songbook number -> video id, for every song that has one."""
    found = {}
    for root in (*additional, cache):
        for meta_path in root.glob("*/meta.json"):
            meta = read(meta_path)
            if meta and isinstance(meta.get("number"), int):
                found[meta["number"]] = meta_path.parent.name
    return found

def assign(cache: Path, meta: dict, *additional: Path) -> int:
    """Give `meta` the next free number unless it already has one; the caller saves it."""
    if not isinstance(meta.get("number"), int):
        meta["number"] = max(numbers(cache, *additional), default=FIRST - 1) + 1
    return meta["number"]

def find(cache: Path, number: int, *additional: Path):
    """(video id, meta) for a songbook number, or None."""
    video_id = numbers(cache, *additional).get(number)
    if not video_id:
        return None
    for root in (cache, *additional):
        if meta := read(root / video_id / "meta.json"):
            return video_id, meta
    return None

def backfill(cache: Path, prepared) -> None:
    """Number prepared songs that predate the songbook, oldest first. Their timestamps are kept,
    since a song's meta.json time is when it was prepared."""
    for meta_path in sorted(cache.glob("*/meta.json"), key=lambda p: p.stat().st_mtime):
        meta = read(meta_path)
        if not meta or isinstance(meta.get("number"), int) or not (meta.get("artist") and meta.get("title")) or not prepared(meta_path.parent):
            continue
        stat = meta_path.stat()
        assign(cache, meta)
        temporary = meta_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(meta_path)
        os.utime(meta_path, (stat.st_atime, stat.st_mtime))
