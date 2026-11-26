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
MUTABLE = {"meta.json", "lyrics.lrc", "timed.json"}
REQUIRED_ROOT = Path("/Volumes/Karaoke Shared")
locks = {}
locks_guard = threading.Lock()
publication_cache = {}
publication_versions = {}
publication_cache_guard = threading.Lock()


def available(root: Path) -> bool:
    """Accept only the required mounted Windows share, never a local substitute."""
    return root == REQUIRED_ROOT and root.is_dir() and os.path.ismount(root)


def _lock(recording_id: str):
    with locks_guard:
        return locks.setdefault(recording_id, threading.Lock())


def _remember(root: Path, recording_id: str):
    with publication_cache_guard:
        publication_cache.setdefault(root, set()).add(recording_id)
        publication_versions[(root, recording_id)] = time.monotonic_ns()


def known(root: Path) -> dict[str, int] | None:
    """Return the in-process publication index after startup has populated it."""
    with publication_cache_guard:
        ids = publication_cache.get(root)
        return None if ids is None else {
            recording_id: publication_versions[(root, recording_id)] for recording_id in ids
        }


def _atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_bytes(path: Path, value: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(value)
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


def shared_manifest(root: Path, recording_id: str, verify_hashes=True, strict_io=False) -> dict | None:
    """Return a valid published manifest without requiring a retained controller cache."""
    try:
        if not available(root) or not recording_id or any(
                char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in recording_id):
            return None
        package = root / "imported" / recording_id
        embedded = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
        external = json.loads((root / "manifests" / f"{recording_id}.json").read_text(encoding="utf-8"))
        if embedded != external or embedded.get("version") != 1 or embedded.get("complete") is not True \
                or embedded.get("id") != recording_id or not isinstance(embedded.get("artifacts"), dict):
            return None
        artifacts = embedded["artifacts"]
        shared_entries = list(package.iterdir())
        if any(not entry.is_file() for entry in shared_entries) \
                or {entry.name for entry in shared_entries} != {*artifacts, "manifest.json"}:
            return None
        if not REQUIRED.issubset(artifacts):
            return None
        meta = songbook.read(package / "meta.json") or {}
        for field in ("artist", "title", "provider"):
            if not meta.get(field) or embedded.get(field) != meta[field]:
                return None
        for name, expected in artifacts.items():
            shared = package / name
            if not isinstance(expected, dict) or not isinstance(expected.get("bytes"), int) \
                    or not isinstance(expected.get("sha256"), str):
                return None
            if shared.stat().st_size != expected["bytes"]:
                return None
            if verify_hashes and _sha256(shared) != expected["sha256"]:
                return None
        return embedded
    except (FileNotFoundError, NotADirectoryError, ValueError, TypeError, AttributeError):
        return None
    except OSError:
        if strict_io:
            raise
        return None


def validate_shared(root: Path, recording_id: str, verify_hashes=True) -> bool:
    return shared_manifest(root, recording_id, verify_hashes) is not None


def validate(folder: Path, root: Path, verify_shared_hashes=True) -> bool:
    """Verify local artifacts against a valid publication on the required share."""
    manifest = shared_manifest(root, folder.name, verify_shared_hashes)
    if manifest is None:
        return False
    try:
        local_files = {path.name: path for path in _files(folder)}
        artifacts = manifest["artifacts"]
        if not REQUIRED.issubset(local_files) or set(artifacts) != set(local_files):
            return False
        return all(source.stat().st_size == artifacts[name]["bytes"]
                   and _sha256(source) == artifacts[name]["sha256"] for name, source in local_files.items())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def published(root: Path, verify_hashes=False) -> set[str]:
    """IDs of complete packages that are independently valid on the share."""
    if not available(root):
        return set()
    result = {path.stem for path in (root / "manifests").glob("*.json")
              if shared_manifest(root, path.stem, verify_hashes, strict_io=True) is not None}
    with publication_cache_guard:
        previous = publication_cache.get(root, set())
        publication_cache[root] = result
        for recording_id in result:
            publication_versions.setdefault((root, recording_id), time.monotonic_ns())
        for recording_id in previous - result:
            publication_versions.pop((root, recording_id), None)
    return result


def stage(root: Path, recording_id: str, destination: Path) -> Path:
    """Copy a verified shared package into temporary controller working storage."""
    manifest = shared_manifest(root, recording_id, True)
    if manifest is None:
        raise RuntimeError("Published package failed checksum validation")
    if destination.exists():
        if validate(destination, root, verify_shared_hashes=False):
            return destination
        raise RuntimeError("Local working files already exist for this song")
    temporary = destination.parent / f".{recording_id}.{uuid4().hex}.stage"
    try:
        temporary.mkdir(parents=True)
        package = root / "imported" / recording_id
        for name in manifest["artifacts"]:
            shutil.copy2(package / name, temporary / name)
        temporary.replace(destination)
        return destination
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def delete(root: Path, recording_id: str):
    """Delete the authoritative shared package and its records."""
    if not recording_id or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-" for char in recording_id):
        raise ValueError("Invalid recording ID for shared deletion")
    if not available(root):
        raise RuntimeError(f"Windows share is unavailable: {root}")
    with _lock(recording_id):
        package = root / "imported" / recording_id
        manifest = root / "manifests" / f"{recording_id}.json"
        if not package.exists() and not manifest.exists():
            raise FileNotFoundError(recording_id)
        removed = root / "imported" / f".{recording_id}.{uuid4().hex}.deleted"
        previous_manifest = manifest.read_bytes() if manifest.is_file() else None
        cleanup_removed = False
        try:
            if package.exists():
                package.replace(removed)
            manifest.unlink(missing_ok=True)
            try:
                (root / "failed" / f"{recording_id}.json").unlink(missing_ok=True)
            except OSError:
                pass
            cleanup_removed = True
        except Exception as delete_error:
            try:
                if previous_manifest is not None:
                    _atomic_bytes(manifest, previous_manifest)
                if removed.exists() and not package.exists():
                    removed.replace(package)
                cleanup_removed = True
            except Exception as rollback_error:
                raise RuntimeError(f"Shared deletion failed and rollback could not complete; recovery copy retained at {removed}: {rollback_error}") from delete_error
            raise
        finally:
            if cleanup_removed:
                shutil.rmtree(removed, ignore_errors=True)
        with publication_cache_guard:
            publication_cache.setdefault(root, set()).discard(recording_id)
            publication_versions.pop((root, recording_id), None)


def record_failure(root: Path, recording_id: str, meta: dict, error: Exception):
    _atomic_json(root / "failed" / f"{recording_id}.json", {
        "id": recording_id, "artist": meta.get("artist"), "title": meta.get("title"),
        "failed_at": time.time(), "error": str(error),
    })


def _metadata_update(folder: Path, root: Path, existing: dict, meta: dict, sources: list[Path]) -> dict | None:
    """Update small metadata artifacts without renaming media files that a player may have open."""
    source_map = {path.name: path for path in sources}
    artifacts = existing["artifacts"]
    if (set(source_map) ^ set(artifacts)) - MUTABLE:
        return None
    for name, source in source_map.items():
        if name in MUTABLE:
            continue
        expected = artifacts.get(name)
        if not expected or source.stat().st_size != expected["bytes"] or _sha256(source) != expected["sha256"]:
            return None
    updated_artifacts = {name: value for name, value in artifacts.items() if name not in MUTABLE}
    for name in MUTABLE:
        if source := source_map.get(name):
            updated_artifacts[name] = {"bytes": source.stat().st_size, "sha256": _sha256(source)}
    manifest = {"version": 1, "complete": True, "id": folder.name,
                "artist": meta.get("artist"), "title": meta.get("title"),
                "provider": meta.get("provider"), "exported_at": time.time(),
                "artifacts": updated_artifacts}
    package = root / "imported" / folder.name
    external_path = root / "manifests" / f"{folder.name}.json"
    embedded_path = package / "manifest.json"
    backup = root / "imported" / f".{folder.name}.{uuid4().hex}.metadata-old"
    temporary = {}
    installed = set()
    backed_up = set()
    cleanup_backup = False
    previous_embedded = embedded_path.read_bytes()
    previous_external = external_path.read_bytes()
    try:
        backup.mkdir()
        for name in MUTABLE:
            target = package / name
            if source := source_map.get(name):
                staged = package / f".{name}.{uuid4().hex}.tmp"
                temporary[name] = staged
                shutil.copy2(source, staged)
            if target.exists():
                target.replace(backup / name)
                backed_up.add(name)
            if name in temporary:
                temporary[name].replace(target)
                installed.add(name)
        _atomic_json(embedded_path, manifest)
        _atomic_json(external_path, manifest)
        if not validate(folder, root):
            raise RuntimeError("Published package failed checksum validation")
        try:
            (root / "failed" / f"{folder.name}.json").unlink(missing_ok=True)
        except OSError:
            pass  # the verified package is authoritative; stale failure cleanup can retry later
        _remember(root, folder.name)
        cleanup_backup = True
        return manifest
    except Exception as publish_error:
        try:
            for name in installed:
                (package / name).unlink(missing_ok=True)
            for name in backed_up:
                saved = backup / name
                if saved.exists():
                    saved.replace(package / name)
            _atomic_bytes(embedded_path, previous_embedded)
            _atomic_bytes(external_path, previous_external)
            cleanup_backup = True
        except Exception as rollback_error:
            raise RuntimeError(f"Metadata publication failed and rollback could not complete; backup retained at {backup}: {rollback_error}") from publish_error
        raise
    finally:
        for staged in temporary.values():
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        if cleanup_backup:
            shutil.rmtree(backup, ignore_errors=True)


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
            if not all(meta.get(field) for field in ("artist", "title", "provider")):
                raise RuntimeError("Karaoke package metadata is incomplete")
            incoming.mkdir(parents=True, exist_ok=True)
            manifests.mkdir(parents=True, exist_ok=True)
            sources = _files(folder)
            existing = shared_manifest(root, recording_id, verify_hashes=True, strict_io=True)
            if existing and (updated := _metadata_update(folder, root, existing, meta, sources)) is not None:
                return updated
            previous_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None
            staging.mkdir()
            artifacts = {}
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
            _remember(root, recording_id)
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


def reconcile(cache: Path, root: Path | None, verify_shared_hashes=True) -> set[str]:
    """Repair publications when possible and return only recording IDs verified on the share."""
    if root is None or not available(root):
        return set()
    verified = published(root, verify_shared_hashes)
    for meta_path in sorted(cache.glob("*/meta.json")):
        folder = meta_path.parent
        if not stems.prepared(folder):
            continue
        try:
            if validate(folder, root, verify_shared_hashes=verify_shared_hashes):
                verified.add(folder.name)
            else:
                publish(folder, root)
                verified.add(folder.name)
        except Exception:
            continue
    return verified
