"""Atomic publication of completed karaoke packages to a shared folder."""
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from uuid import uuid4

from . import songbook, stems


ARTIFACTS = {"audio.m4a", "video.mp4", "lyrics.lrc", "timed.json", "meta.json"}
REQUIRED = {"audio.m4a", "meta.json", "vocals.opus", "no_vocals.opus"}
REQUIRED_ROOT = Path("/Volumes/Karaoke Shared")
locks = {}
locks_guard = threading.Lock()


def available(root: Path) -> bool:
    """Accept only the required mounted Windows share, never a local substitute."""
    return root == REQUIRED_ROOT and root.is_dir() and os.path.ismount(root)


def _lock(recording_id: str):
    with locks_guard:
        return locks.setdefault(recording_id, threading.Lock())


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _files(folder: Path) -> list[Path]:
    names = set(ARTIFACTS)
    for stem in stems.NAMES:
        path = stems.path(folder, stem)
        if path:
            names.add(path.name)
    return [folder / name for name in sorted(names) if (folder / name).is_file()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as opened:
        for chunk in iter(lambda: opened.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(folder: Path, root: Path) -> bool:
    """Verify that the cache, shared package, and both manifests describe identical artifacts."""
    try:
        if not available(root):
            return False
        recording_id = folder.name
        package = root / "imported" / recording_id
        embedded = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        external = json.loads((root / "manifests" / f"{recording_id}.json").read_text(encoding="utf-8"))
        if embedded != external or embedded.get("version") != 1 or embedded.get("complete") is not True \
                or embedded.get("id") != recording_id or not isinstance(embedded.get("artifacts"), dict):
            return False
        local_files = {path.name: path for path in _files(folder)}
        artifacts = embedded["artifacts"]
        if not REQUIRED.issubset(local_files) or set(artifacts) != set(local_files):
            return False
        shared_entries = list(package.iterdir())
        if any(not entry.is_file() for entry in shared_entries) \
                or {entry.name for entry in shared_entries} != {*artifacts, "manifest.json"}:
            return False
        for name, source in local_files.items():
            shared = package / name
            expected = artifacts.get(name)
            if not isinstance(expected, dict) or not isinstance(expected.get("bytes"), int) \
                    or not isinstance(expected.get("sha256"), str):
                return False
            if source.stat().st_size != expected["bytes"] or shared.stat().st_size != expected["bytes"]:
                return False
            if _sha256(source) != expected["sha256"] or _sha256(shared) != expected["sha256"]:
                return False
        return True
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def record_failure(root: Path, recording_id: str, meta: dict, error: Exception):
    _atomic_json(root / "failed" / f"{recording_id}.json", {
        "id": recording_id, "artist": meta.get("artist"), "title": meta.get("title"),
        "failed_at": time.time(), "error": str(error),
    })


def publish(folder: Path, root: Path) -> dict:
    """Copy one complete cache folder, then expose it with a same-share rename."""
    recording_id = folder.name
    if not recording_id or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in recording_id):
        raise ValueError("Invalid recording ID for shared publication")
    if not available(root):
        raise RuntimeError(f"Windows share is unavailable: {root}")
    meta = songbook.read(folder / "meta.json") or {}
    with _lock(recording_id):
        incoming = root / "imported"
        manifests = root / "manifests"
        staging = incoming / f".{recording_id}.{uuid4().hex}.tmp"
        previous = None
        destination = incoming / recording_id
        manifest_path = manifests / f"{recording_id}.json"
        previous_manifest = None
        exposed = False
        try:
            incoming.mkdir(parents=True, exist_ok=True)
            manifests.mkdir(parents=True, exist_ok=True)
            previous_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
            staging.mkdir()
            artifacts = {}
            sources = _files(folder)
            if not REQUIRED.issubset(path.name for path in sources):
                raise RuntimeError("Karaoke package is incomplete")
            for source in sources:
                artifact_path = staging / source.name
                shutil.copy2(source, artifact_path)
                artifacts[source.name] = {"bytes": artifact_path.stat().st_size, "sha256": _sha256(artifact_path)}
            if not stems.ready(staging) or "meta.json" not in artifacts:
                raise RuntimeError("Karaoke package is incomplete")
            manifest = {"version": 1, "complete": True, "id": recording_id,
                        "artist": meta.get("artist"), "title": meta.get("title"),
                        "provider": meta.get("provider"), "exported_at": time.time(), "artifacts": artifacts}
            (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            if destination.exists():
                previous = incoming / f".{recording_id}.{uuid4().hex}.old"
                destination.replace(previous)
            try:
                staging.replace(destination)
            except Exception:
                if previous and previous.exists() and not destination.exists():
                    previous.replace(destination)
                raise
            exposed = True
            _atomic_json(manifest_path, manifest)
            if not validate(folder, root):
                raise RuntimeError("Published package failed checksum validation")
            for old in incoming.glob(f".{recording_id}.*.old"):
                shutil.rmtree(old, ignore_errors=True)
            (root / "failed" / f"{recording_id}.json").unlink(missing_ok=True)
            return manifest
        except Exception as exc:
            if exposed:
                shutil.rmtree(destination, ignore_errors=True)
                if previous and previous.exists():
                    previous.replace(destination)
                try:
                    if previous_manifest is None:
                        manifest_path.unlink(missing_ok=True)
                    else:
                        temporary_manifest = manifests / f".{manifest_path.name}.{uuid4().hex}.tmp"
                        temporary_manifest.write_bytes(previous_manifest)
                        temporary_manifest.replace(manifest_path)
                finally:
                    if 'temporary_manifest' in locals():
                        temporary_manifest.unlink(missing_ok=True)
            try:
                record_failure(root, recording_id, meta, exc)
            except Exception:
                pass
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def backfill(cache: Path, root: Path) -> dict:
    published = failed = 0
    for meta_path in sorted(cache.glob("*/meta.json")):
        if not stems.prepared(meta_path.parent):
            continue
        if validate(meta_path.parent, root):
            continue
        try:
            publish(meta_path.parent, root)
            published += 1
        except Exception:
            failed += 1
    return {"published": published, "failed": failed}


def reconcile(cache: Path, root: Path | None) -> set[str]:
    """Repair publications when possible and return only recording IDs verified on the share."""
    if root is None or not available(root):
        return set()
    verified = set()
    for meta_path in sorted(cache.glob("*/meta.json")):
        folder = meta_path.parent
        if not stems.prepared(folder):
            continue
        try:
            if validate(folder, root):
                verified.add(folder.name)
            else:
                publish(folder, root)
                verified.add(folder.name)
        except Exception:
            continue
    return verified
