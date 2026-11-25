"""Local and authenticated LAN renderer clients."""
import hashlib
import ipaddress
import math
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


MAX_RENDER_SECONDS = 2 * 60 * 60


def normalize_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use an http(s) renderer URL such as http://192.168.1.42:8790")
    if parsed.path not in ("", "/"):
        raise ValueError("Renderer URL must not contain a path")
    try:
        address = ipaddress.ip_address(parsed.hostname or "")
    except ValueError as exc:
        raise ValueError("Use a literal private IP address, not a hostname") from exc
    shared = address in ipaddress.ip_network("100.64.0.0/10")
    if not (address.is_private or shared) or address.is_multicast or address.is_unspecified or address.is_link_local:
        raise ValueError("Renderer must use a private LAN or Tailscale IP address")
    try:
        port = parsed.port or 8790
    except ValueError as exc:
        raise ValueError("Renderer port must be a number from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Renderer port must be a number from 1 to 65535")
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"{parsed.scheme}://{host}:{port}"


def _headers() -> dict[str, str]:
    token = os.environ.get("KTVIBES_RENDERER_TOKEN")
    return {"X-KTVibes-Renderer-Token": token} if token else {}


def health(renderer: dict) -> dict:
    if renderer["id"] == "local":
        from .separate import backend_info
        return {"machine_id": "local", **backend_info(), "busy": False}
    import httpx
    response = httpx.get(renderer["url"] + "/v1/health", headers=_headers(), timeout=10, follow_redirects=False)
    response.raise_for_status()
    data = response.json()
    if data.get("protocol") != 1 or not isinstance(data.get("machine_id"), str):
        raise RuntimeError("Renderer returned an invalid identity")
    return data


def separate(source: Path, destination: Path, progress, library, renderer_id=None) -> tuple[float, float]:
    renderer = library.renderer(renderer_id) if library and renderer_id else (library.selected_renderer() if library else {"id": "local"})
    if renderer["id"] == "local":
        from .separate import separate as local_separate
        return local_separate(source, destination, progress)
    return _remote_separate(renderer, source, destination, progress, library)


def _remote_separate(renderer: dict, source: Path, destination: Path, progress, library) -> tuple[float, float]:
    import httpx
    import soundfile as sf
    started = time.monotonic()
    source_digest = hashlib.sha256()
    source_digest.update(b"htdemucs-opus-v1\0")
    with source.open("rb") as input_file:
        for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
            source_digest.update(chunk)
    headers = {**_headers(), "Content-Type": "application/octet-stream", "X-KTVibes-Job-Key": source_digest.hexdigest()}
    with source.open("rb") as audio, httpx.Client(timeout=httpx.Timeout(60, read=300), follow_redirects=False) as client:
        identity = health(renderer)["machine_id"]
        library.renderer_seen(renderer["id"], identity)
        response = client.post(renderer["url"] + "/v1/jobs", headers=headers, content=audio)
        response.raise_for_status()
        job_id = response.json()["id"]
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise RuntimeError("Renderer returned an invalid job ID")
        try:
            while True:
                if time.monotonic() - started > MAX_RENDER_SECONDS:
                    raise TimeoutError("Remote renderer did not finish within two hours")
                status_response = client.get(f"{renderer['url']}/v1/jobs/{job_id}", headers=_headers())
                status_response.raise_for_status()
                job = status_response.json()
                if progress and isinstance(job.get("progress"), (int, float)):
                    progress(float(job["progress"]))
                if job.get("status") == "failed":
                    raise RuntimeError(job.get("error") or "Remote separation failed")
                if job.get("status") == "ready":
                    break
                time.sleep(0.5)
            if job.get("machine_id") != identity:
                raise RuntimeError("Renderer identity changed while the job was running")
            if job.get("protocol") != 1:
                raise RuntimeError("Renderer protocol is incompatible with this KTVibes controller")
            try:
                gain = float(job["gain"])
                seconds = float(job["seconds"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("Renderer returned invalid separation metadata") from exc
            if not math.isfinite(gain) or not 0 < gain <= 100 or not math.isfinite(seconds) or seconds < 0:
                raise RuntimeError("Renderer returned invalid separation metadata")
            manifest = job.get("artifacts") or {}
            temporary = []
            try:
                destination.mkdir(parents=True, exist_ok=True)
                for name in ("vocals.opus", "no_vocals.opus"):
                    expected = manifest.get(name, {}).get("sha256")
                    expected_bytes = manifest.get(name, {}).get("bytes")
                    if not expected or not isinstance(expected_bytes, int) or not 0 < expected_bytes <= 500 * 1024 * 1024:
                        raise RuntimeError(f"Renderer did not provide {name}")
                    target = destination / f".{name}.remote-{uuid4().hex}.tmp"
                    temporary.append((target, destination / name))
                    digest = hashlib.sha256()
                    received = 0
                    with client.stream("GET", f"{renderer['url']}/v1/jobs/{job_id}/artifacts/{name}", headers=_headers()) as artifact:
                        artifact.raise_for_status()
                        with target.open("wb") as output:
                            for chunk in artifact.iter_bytes():
                                received += len(chunk)
                                if received > expected_bytes:
                                    raise RuntimeError(f"Renderer sent too much data for {name}")
                                digest.update(chunk)
                                output.write(chunk)
                    if received != expected_bytes or digest.hexdigest() != expected:
                        raise RuntimeError(f"Renderer checksum failed for {name}")
                durations = [sf.info(source_path).duration for source_path, _ in temporary]
                if any(duration <= 0 for duration in durations) or abs(durations[0] - durations[1]) > 1:
                    raise RuntimeError("Renderer returned invalid or mismatched audio stems")
                marker = destination / ".stems-incomplete"
                marker.touch()
                for source_path, target in temporary:
                    source_path.replace(target)
                marker.unlink()
            finally:
                for source_path, _ in temporary:
                    source_path.unlink(missing_ok=True)
        finally:
            try:
                client.delete(f"{renderer['url']}/v1/jobs/{job_id}", headers=_headers())
            except Exception:
                pass
        return seconds or time.monotonic() - started, gain
