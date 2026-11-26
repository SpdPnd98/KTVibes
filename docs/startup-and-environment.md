# Starting KTVibes and configuring environment variables

This deployment uses two machines:

- The Mac runs the KTVibes controller, downloads media, serves the TV and phone pages, maintains the queue, and publishes completed packages.
- The RTX renderer at `10.8.0.40:8790` runs separation, lyric alignment, full-audio analysis, and pronunciation-guide generation.

Start the renderer first, confirm its health from the Mac, and then start the controller.

## Quick controller start

Open Terminal on the Mac and run:

```bash
cd /Users/teebryan/Codes/KTVibes-custom

export KTVIBES_CACHE="/Users/teebryan/Codes/KTVibes-custom/cache"
export KTVIBES_IMPORT_ROOT="/Volumes/Karaoke Shared/incoming"
export KTVIBES_SHARE_ROOT="/Volumes/Karaoke Shared"
export KTVIBES_KEEP_LOCAL_CACHE=0
export KTVIBES_RENDERER_TOKEN="replace-with-the-renderer-token"
export KTVIBES_OWNER_TOKEN="replace-with-a-separate-owner-token"
export KTVIBES_OPEN_BROWSER=1

uv sync
uv run ktvibes
```

Keep that terminal open. Stop KTVibes with `Ctrl+C`.

The pages are:

- TV: `http://127.0.0.1:8765/`
- Phone remote: `http://127.0.0.1:8765/remote`
- Backstage: `http://127.0.0.1:8765/owner`

If `KTVIBES_OPEN_BROWSER` is unset, open the TV URL manually.

## Reusable controller environment

Shell `export` commands apply only to the current terminal. For repeatable starts, keep the values in a private file outside the repository, for example `~/.config/ktvibes/controller.env`:

```bash
mkdir -p "$HOME/.config/ktvibes"
nano "$HOME/.config/ktvibes/controller.env"
```

Use these contents, replacing both token placeholders:

```bash
export KTVIBES_CACHE="/Users/teebryan/Codes/KTVibes-custom/cache"
export KTVIBES_IMPORT_ROOT="/Volumes/Karaoke Shared/incoming"
export KTVIBES_SHARE_ROOT="/Volumes/Karaoke Shared"
export KTVIBES_KEEP_LOCAL_CACHE=0
export KTVIBES_RENDERER_TOKEN="replace-with-the-renderer-token"
export KTVIBES_OWNER_TOKEN="replace-with-a-separate-owner-token"
export KTVIBES_OPEN_BROWSER=1

# Optional when the QR code chooses the wrong Mac network address:
# export KTVIBES_REMOTE_URL="http://192.168.0.x:8765/remote"

# Optional YouTube browser login, if bot protection blocks downloads:
# export KTVIBES_COOKIES_FROM_BROWSER="chrome"
```

Protect the file because it contains tokens:

```bash
chmod 600 "$HOME/.config/ktvibes/controller.env"
```

Start the controller with:

```bash
cd /Users/teebryan/Codes/KTVibes-custom
source "$HOME/.config/ktvibes/controller.env"
uv sync
uv run ktvibes
```

KTVibes does not automatically read `.env` files. The `source` command is required unless the variables are exported another way.

## Start the RTX renderer

The renderer and controller must use the same `KTVIBES_RENDERER_TOKEN`. Do not reuse the owner token.

Update the renderer checkout before starting it whenever the controller protocol changes. The current controller requires the renderer to report `separation`, `alignment`, and `guides` capabilities.

### Linux or WSL shell

Run on the RTX machine from its KTVibes checkout:

```bash
export KTVIBES_RENDERER_TOKEN="replace-with-the-renderer-token"
export KTVIBES_RENDERER_PORT=8790
export KTVIBES_RENDERER_CACHE="$HOME/.cache/ktvibes-renderer"

uv sync
uv run ktvibes-renderer
```

### Windows PowerShell

Run on the RTX machine from its KTVibes checkout:

```powershell
$env:KTVIBES_RENDERER_TOKEN = "replace-with-the-renderer-token"
$env:KTVIBES_RENDERER_PORT = "8790"
$env:KTVIBES_RENDERER_CACHE = "$HOME\.cache\ktvibes-renderer"

uv sync
uv run ktvibes-renderer
```

Keep the renderer terminal open. Stop it with `Ctrl+C`.

## Verify the renderer before starting work

From the Mac, after loading the controller environment:

```bash
curl --fail --show-error \
  -H "X-KTVibes-Renderer-Token: $KTVIBES_RENDERER_TOKEN" \
  http://10.8.0.40:8790/v1/health
```

A usable response has:

- `"ready": true`
- `"device": "cuda"`
- `"capabilities": ["separation", "guides", "alignment"]`

If port `22` responds but port `8790` refuses the connection, the machine is online but `ktvibes-renderer` is not running or is blocked by its firewall.

In Backstage, add or select the renderer URL `http://10.8.0.40:8790`. Do not select **Local machine** for production preparation on this Mac.

## Verify the required Windows share

Completed packages are valid only under the mounted Windows share. The application intentionally rejects a different output path or an ordinary local directory with the same name.

Before starting the controller, verify the mount:

```bash
mount | grep -F "/Volumes/Karaoke Shared"
df -h "/Volumes/Karaoke Shared"
ls "/Volumes/Karaoke Shared"
```

The required folders are:

```text
/Volumes/Karaoke Shared/incoming/
/Volumes/Karaoke Shared/imported/
/Volumes/Karaoke Shared/manifests/
/Volumes/Karaoke Shared/failed/
```

At startup, KTVibes discovers complete packages from matching embedded and external manifests on the share and confirms every declared shared file and size. Shared artifact bytes are fully checksummed when published and during an explicit full reconciliation, but are not reread over SMB on every restart. With local retention disabled, playback, the songbook, lyrics, and ambient video are read from `imported/`.

## Environment variable reference

| Variable | Machine | Required here | Purpose |
|---|---|---:|---|
| `KTVIBES_CACHE` | Controller | Recommended | Temporary preparation work, `queue.json`, and `library.sqlite3`. |
| `KTVIBES_KEEP_LOCAL_CACHE` | Controller | No | Set to `1` to retain completed package copies locally. Default `0` removes them after verified publication. |
| `KTVIBES_IMPORT_ROOT` | Controller | Recommended | Incoming M4A directory, normally `/Volumes/Karaoke Shared/incoming`. |
| `KTVIBES_SHARE_ROOT` | Controller | Recommended | Must resolve to `/Volumes/Karaoke Shared`; other output roots are rejected. |
| `KTVIBES_RENDERER_TOKEN` | Both | Yes | Shared secret authenticating controller-to-renderer requests. Values must match. |
| `KTVIBES_OWNER_TOKEN` | Controller | Recommended | Protects Backstage when accessed from another machine. Use a different token from the renderer token. |
| `KTVIBES_PORT` | Controller | No | Controller port; default `8765`. |
| `KTVIBES_REMOTE_URL` | Controller | No | Overrides the URL encoded in the phone QR code. |
| `KTVIBES_OPEN_BROWSER` | Controller | No | Set to `1` to open the TV page after startup. |
| `KTVIBES_COOKIES_FROM_BROWSER` | Controller | No | Browser whose YouTube cookies yt-dlp may use, such as `chrome`. |
| `KTVIBES_VIDEO_HEIGHT` | Controller | No | Maximum downloaded video height; default `720`. |
| `KTVIBES_NODE` | Controller | No | Path to Node.js 22+ when it is not the `node` found on `PATH`; common Homebrew `node@22` paths are detected automatically. |
| `KTVIBES_RENDERER_PORT` | Renderer | No | Renderer port; default `8790`. |
| `KTVIBES_RENDERER_CACHE` | Renderer | Recommended | Renderer upload, job, and recovery storage. |

## Generate secure tokens

Generate independent random values on the Mac:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Use one value for `KTVIBES_RENDERER_TOKEN` on both machines. Use the other value for `KTVIBES_OWNER_TOKEN`. Do not commit either token to Git.

## Common restart checks

### Controller port is already in use

```bash
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

Stop the existing process or intentionally choose another `KTVIBES_PORT`.

### Renderer is unavailable

```bash
nc -z -w 3 10.8.0.40 22
nc -z -w 3 10.8.0.40 8790
```

If only port `22` works, log in to the renderer and start `uv run ktvibes-renderer`.

### Preparation should not start

Do not resume a large queue when the Mac cache disk is nearly full. Check it with:

```bash
df -h /Users/teebryan/Codes/KTVibes-custom
```

Downloads, remuxing, active preparation, and publication staging still require temporary Mac disk space even when completed copies are not retained and model inference runs on the RTX renderer.

The persisted curated queue resumes automatically when the controller starts. Confirm the renderer, share, and free disk are healthy before starting the controller if preparation jobs are queued.

### Inspect controller tests before restart

```bash
cd /Users/teebryan/Codes/KTVibes-custom
uv run pytest
```

The controller and renderer should be restarted after code or protocol changes.
