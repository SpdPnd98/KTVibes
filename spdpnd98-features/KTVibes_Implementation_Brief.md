# KTVibes — Home Karaoke Library & AMD Acceleration

Implementation brief for OpenCode | 25 November 2026

## 1. Objective
Extend the existing KTVibes application into a low-maintenance, self-hosted home karaoke system. Automatically discover and curate candidate songs, let the owner review them in batches, prepare approved tracks ahead of karaoke sessions, and preserve corrected lyrics and pronunciation. Retain the existing phone remote, shared queue, synchronized lyrics, adjustable original vocals and TV playback. No mandatory paid APIs or subscriptions; use local processing and media the owner is authorized to use.

## 2. Instructions to OpenCode
Work in the existing KTVibes checkout, not a replacement application. First inspect the repository, record the commit SHA, read its README, dependency/lock files, tests, installation scripts and implementation notes. Trace search, queue, preparation, cache, lyric alignment, pronunciation and playback paths. Identify actual extension points and propose a short implementation plan before editing. The paths and interfaces below are proposals unless verified in the checkout. Keep changes incremental, use the existing UI stack and conventions, and avoid unrelated refactors. Do not silently overwrite existing caches or user edits. Run relevant tests after each phase; report failures and untested hardware explicitly.

## 3. Existing baseline
Upstream README describes a phone remote and TV player, Demucs htdemucs separation, LRCLIB lyrics, MMS forced alignment, pronunciation guides (including Japanese romaji), per-song cache under `cache/<youtube_id>/`, and CUDA/MPS/CPU support. It identifies `ktvibes/main.py`, `queue.py`, `worker.py`, `youtube.py`, `lyrics.py`, and `separate.py` as relevant modules. Verify all of these in the local revision. AMD acceleration is a proposed extension, not a confirmed upstream feature.

## 4. MVP user workflow
1. Import a CSV of title/artist/language and optional source ID, a supported playlist export, or a local authorized-media folder. Optionally discover candidates from selected artists or community catalogs later.
2. Resolve likely recording and lyric matches without downloading or processing every candidate. Show alternatives and confidence explanations.
3. Review a persistent list: approve, reject, defer, change source, edit metadata; allow bulk approval of selected rows. Never auto-approve uncertain matches.
4. Start a preparation batch on demand. Display progress, stage, error and retry controls. Pause bulk work during interactive preparation if necessary.
5. Review lyrics and Japanese pronunciation; preview against audio and save corrections. Mark ready songs as reviewed.
6. Search the local library by title, artist, language or aliases; queue prepared songs from the existing remote without repeating preparation.

## 5. Data and architecture
Prefer existing persistence if suitable; otherwise add SQLite with migrations and transactions. Suggested logical entities:

| Entity | Fields (illustrative) |
|---|---|
| Song | id, title, artist, language, aliases, duration, created_at |
| Recording | id, song_id, provider, external_id, source reference, duration, provenance |
| Candidate | id, recording_id, match score/reasons, lyric availability, review status |
| PreparationJob | id, recording_id, stage, status, attempts, error, timestamps, pipeline version |
| Artifact | recording_id, kind, path, checksum, generation/version |
| LyricRevision | id, recording_id, base revision, source, text/timing, pronunciation overrides, reviewed_at |

Separate review status (pending/approved/rejected/deferred) from processing status (not_started/queued/running/ready/failed/cancelled). Unique provider+external_id for exact duplicate prevention; normalized artist/title/duration only suggest near duplicates, never merge live/remix/cover versions silently. Use Unicode normalization for search while retaining original display text. Persist job state across restarts; recover interrupted jobs safely.

### Proposed interfaces (adapt to upstream)
- `curate import --csv favourites.csv` and `curate list --status pending`.
- `curate prepare --approved-only --workers 1` and `curate retry --failed`.
- Library/review page with search, filters, bulk selection, source alternatives, progress, retry and export CSV.
- API for import/list/review/job management and lyric revisions; reuse existing API/WebSocket patterns. Do not expose raw filesystem paths or arbitrary command execution; validate URLs and local imports, restrict LAN admin actions, and retain existing remote security model or document its limitations.

## 6. Discovery and matching
Start with CSV and existing search provider to avoid new service dependencies. Optional adapters: MusicBrainz for metadata, LRCLIB for synced lyrics, Karaoke Mugen for existing community-prepared candidates. Treat external results as untrusted; respect service limits, licenses and terms. Use authorized local media or permitted source access; never bypass access controls. Score title/artist match, duration tolerance and version indicators (live, cover, remix, instrumental); display reasons and candidates rather than implying the score is a calibrated probability. Rate-limit requests, cache metadata, use timeouts/backoff and allow offline review of previously fetched metadata. Keep provider failures isolated. No paid API keys required for the MVP.

## 7. Preparation and cache
Reuse upstream worker/provider logic. Avoid creating an independent media pipeline unless necessary. Suggested stages: resolve source -> acquire authorized media -> separate vocals/instrumental -> resolve lyrics -> align -> generate pronunciation -> validate -> publish. Use existing artifact formats and playback contracts. Default one GPU job at a time; bounded CPU/network concurrency, cancellation, timeouts and retry only transient errors. Give live karaoke queue priority; don't let a bulk batch starve playback. Stage files in temporary locations, atomically publish completed artifacts, verify required files and recover incomplete jobs. Cache keys should include recording identity and relevant stage/model/config versions; rerun only invalidated stages. Keep manual lyrics independent from generated artifacts; never replace them without explicit user approval. Expose storage usage and cleanup of unreferenced artifacts; do not delete reviewed data automatically. Provide backup/export and documented restore (including database, revisions and cache; verify portability before promising it). Offline playback should work for fully prepared songs with no provider calls, where supported by the existing player.

## 8. Japanese lyrics and pronunciation
Problem: automated kanji readings may be wrong, especially names, unusual readings and lyrics with deliberate pronunciation variants. Use the existing pronunciation system first; inspect whether upstream supports ruby/furigana or only romaji. Add optional kana/furigana mode if absent, and evaluate SudachiPy or MeCab/UniDic only if the baseline is inadequate. Dictionary output is a suggestion, not ground truth. Allow editing lyric lines, timing and pronunciation per occurrence; store stable line/token identifiers or validated spans tied to a lyric revision. A correction of one occurrence must not modify another occurrence of the same kanji. Support multi-character compounds, kana, mixed Latin text, Unicode and repeated chorus lines. Preview synchronized playback with original-vocal slider for review. If text changes, invalidate affected alignment; if only pronunciation changes, preserve audio and timing where compatible. Flag stale overrides when a new source revision cannot be mapped safely. Maintain manual edits through cache refresh, restart and export/import. Keep other languages' existing behavior intact.

## 9. AMD GPU / ROCm implementation
### Constraints
Exact AMD model, OS, driver and ROCm version are not yet specified. Do not assume all Radeon cards or Windows/WSL2 configurations are supported. Consult AMD's compatibility matrix and PyTorch installation instructions for the actual host. Native Linux is a reasonable first test target if supported. Windows/WSL2 are conditional; DirectML is out of scope for MVP. Do not hardcode a ROCm version or install drivers automatically.

### Audit and design
Inspect upstream `pyproject.toml`, uv lock/source selection, installers, device detection, Demucs and alignment dependencies. PyTorch ROCm intentionally exposes the `torch.cuda` interface; GPU code may already work but a CUDA-specific wheel source or dependency pin can prevent it. Preserve existing default installation and CUDA/MPS/CPU behavior. Provide an explicit documented ROCm installation path with compatible torch/torchaudio builds, using a separate dependency profile or environment if necessary. Ensure subsequent `uv sync` does not silently replace ROCm wheels with CUDA wheels. Avoid committing an unverified global lockfile change.

Add a centralized device capability helper or extend the existing one. Suggested configuration: `auto`, `cpu`, `cuda`, `rocm`, `mps` (adapt naming to upstream). `rocm` maps internally to torch device `cuda` only when `torch.version.hip` is present and `torch.cuda.is_available()` succeeds. `cuda` explicitly requested should distinguish NVIDIA from HIP if exposed as distinct options. Auto selection should retain upstream priority and report the actual backend. Explicit unsupported choices should fail with a useful message; auto may fall back to CPU with a visible warning. Configure separation and alignment independently where operators differ; avoid silently retrying every GPU failure on CPU indefinitely. Report backend, GPU name, torch/HIP version and stage timings without secrets.

### Diagnostic example (not a tested installer)
```python
import torch
print('torch:', torch.__version__)
print('HIP:', torch.version.hip)
print('CUDA build:', torch.version.cuda)
print('GPU available:', torch.cuda.is_available())
if torch.cuda.is_available():
    print('device:', torch.cuda.get_device_name(0))
    x = torch.ones((256, 256), device='cuda')
    print((x @ x).mean().item())
    torch.cuda.synchronize()
```
A successful tensor test is not proof that Demucs or MMS works. Run actual separation on an authorized short fixture, validate both output stems, then run alignment and full playback. If alignment fails on HIP, allow CPU alignment while keeping GPU separation. Benchmark same recording/model/settings on CPU and ROCm; report wall time, peak GPU memory where available, output duration and failures. GPU OOM should produce actionable errors; optionally retry with lower-memory supported settings. No blanket performance promises.

## 10. Acceptance criteria and tests
- Import 100 rows with duplicates twice: no duplicate exact recordings or lost review decisions.
- Review state persists across restart; rejected/deferred songs do not enter approved-only batches.
- A failed song does not stop other jobs; retry and interruption recovery do not corrupt completed artifacts.
- An already prepared song queues without repeated separation; playback retains guide-vocal mixing and synchronized lyrics.
- A Japanese fixture contains the same kanji with different contextual readings; editing one occurrence leaves the other unchanged. Corrections survive restart and regeneration.
- Search handles Japanese, Chinese and English titles/aliases; existing remote queue works during batch activity.
- Test device detection using mocked CPU, CUDA, HIP and MPS capabilities; explicit unsupported backend gives actionable error.
- Run real AMD separation/alignment/playback smoke tests only on compatible hardware; otherwise mark as not tested, not passed.
- Verify existing upstream tests and run regression smoke tests for CPU, existing lyric languages, and player behavior. Use local fixtures/mocked providers for automated tests; no required live network or paid service in CI.

## 11. Delivery order
1. Repository audit, baseline tests, and dependency/device audit.
2. Persistent candidate import/review + CSV export (usable independently).
3. Batch preparation integration, cache safety, and library search.
4. Japanese lyric/pronunciation review editor and durable revisions.
5. ROCm install profile, device detection, diagnostics and hardware validation (may run in parallel after audit).
6. Optional discovery providers, richer UI and portable library backup.

For each phase deliver implementation, tests, commands to run, migration/rollback notes and limitations. Keep UI simple and reuse existing frontend. Do not add Spotify/Apple authentication, a new frontend framework, cloud infrastructure, automatic chart polling or a second player unless justified after MVP. Do not perform large automatic downloads or GPU jobs without operator approval.

## 12. Reference links
- KTVibes upstream: https://github.com/andyleenz/KTVibes
- KTVibes implementation notes: https://github.com/andyleenz/KTVibes/blob/main/docs/how-it-works.md (verify path in checkout)
- PyTorch HIP semantics: https://docs.pytorch.org/docs/stable/notes/hip.html
- AMD ROCm documentation and compatibility: https://rocm.docs.amd.com/
- PyTorch install selector: https://pytorch.org/get-started/locally/
- Demucs: https://github.com/facebookresearch/demucs
- LRCLIB: https://lrclib.net/docs
- Karaoke Mugen: https://api.karaokes.moe/server/

## 13. OpenCode kickoff prompt
Read this brief and inspect the existing checkout. Produce a concise gap analysis against the current commit, identify the actual modules and dependency constraints, and propose incremental implementation steps. Start with Phase 1 and Phase 2, preserving current behavior. Run available tests, report blockers and assumptions, and show changed files. Do not claim AMD compatibility until tested on the target hardware. Ask for GPU model and OS when needed for the ROCm setup, but do not block hardware-independent development on that information.
