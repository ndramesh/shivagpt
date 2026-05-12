# ShivaGPT

A dark-mode chat UI for the Ollama models running on your Nvidia DGX Spark
("kailash"). Designed for Safari on macOS — open one URL and chat with
DeepSeek, Qwen, Llama 3, or any other model you've pulled.

## What you get

- Streaming responses (NDJSON over HTTP — works in Safari).
- Local chat history persistence (no login, no server-side DB; everything
  lives in your browser's `localStorage`).
- Per-conversation model + temperature + system prompt overrides.
- Built-in system prompt library (Coder, Tutor, Brainstorm, etc.) plus your
  own saved presets.
- File attachments: drop or paste PDFs, CSVs, images (PNG/JPG/WebP/GIF),
  and text files into the composer. Documents get extracted to text;
  images go to a vision-capable model (auto-switches to `qwen2.5vl`).
- Markdown rendering with syntax-highlighted code blocks and per-block copy.
- Export each conversation as Markdown, plain text, or raw JSON.
- Dark mode only, optimized for desktop Safari but also responsive on iPhone
  (safe-area insets, `visualViewport` height tracking for the iOS dynamic
  toolbar).
- Thin Python proxy on the DGX so the browser only talks to one origin
  (avoids CORS headaches, handles streaming cleanly, and lets you put the
  service behind a reverse proxy later if you want auth).

## Architecture

```
   Safari (Mac)                   kailash (DGX)
  ┌─────────────┐  http :8000   ┌──────────────────┐  http :11434
  │ index.html  │ ────────────► │ server.py (FastAPI│ ──────────────► Ollama
  │  (SPA)      │ ◄──── stream ─│  proxy + static)  │ ◄──── stream ───
  └─────────────┘               └──────────────────┘
```

## Models you'll want pulled on Kailash

```bash
# Text models (any of these, one is required)
ollama pull deepseek-r1:7b      # default
ollama pull llama3:latest
ollama pull qwen2.5:7b

# Vision model (for image attachments — recommended)
ollama pull qwen2.5vl:32b       # ~21 GB; best document/screenshot accuracy
# or the smaller alternative:
# ollama pull qwen2.5vl:7b
```

The frontend's "vision model" setting (Settings → defaults to `qwen2.5vl`)
is matched as a prefix, so it'll use whichever `qwen2.5vl:*` tag you have.

## One-time setup on the DGX

You only need Python 3.9+ on `kailash`. Ollama should already be running
(`systemctl status ollama` or `ollama serve`).

From your Mac:

```bash
cd ~/src/shivagpt
./deploy.sh
```

This will rsync the project to `kailash:~/shivagpt`, create a venv, and
install FastAPI + uvicorn + httpx.

Override host/dir as needed:

```bash
HOST=mybox DIR=~/apps/shivagpt ./deploy.sh
```

## Running the server

**Recommended — install as a systemd service** (starts on boot, auto-restarts):

```bash
./deploy.sh --service
```

This rsyncs the code, sets up the venv, then SSHes in and runs
`install-service.sh` with sudo, which writes
`/etc/systemd/system/shivagpt.service`, enables it, starts it, tails the
journal, and pings `/healthz`. Re-running `./deploy.sh --service` later
will rsync new code and `systemctl restart shivagpt`.

Day-to-day commands on `kailash`:

```bash
sudo systemctl status shivagpt
sudo systemctl restart shivagpt
sudo systemctl stop shivagpt
sudo journalctl -u shivagpt -f         # live logs
sudo systemctl disable --now shivagpt  # uninstall (keeps files)
```

Then in Safari:

```
http://kailash:8000
```

(If `kailash` doesn't resolve from your Mac, use the LAN IP, e.g.
`http://10.0.0.42:8000`.)

### Or, run it manually (no service)

```bash
ssh kailash 'cd ~/shivagpt && ./run.sh'                              # foreground
ssh kailash 'cd ~/shivagpt && nohup ./run.sh > server.log 2>&1 & disown'  # background
```

## Configuration

Set via env vars (see `run.sh`):

- `OLLAMA_URL` — where Ollama lives. Default `http://localhost:11434`.
- `PORT` — server port. Default `8000`.
- `HOST_BIND` — interface to bind. Default `0.0.0.0` (all interfaces).
- `SHIVAGPT_DEBUG` — set to `1` for verbose request + chat-call logs and
  to enable the `/api/debug` introspection endpoint. The systemd unit
  ships with this turned on; flip it off later via `sudo systemctl edit
  shivagpt` adding `Environment=SHIVAGPT_DEBUG=0`.

In-app settings (top-right gear icon):

- **API base URL** — leave blank to use the same origin (recommended).
  Set explicitly if you ever open `index.html` directly from disk.
- **Default model** — populated from `/api/tags`. The default of
  `deepseek` will resolve to whatever DeepSeek tag you have installed
  (`deepseek-r1:7b`, etc.) via prefix match.
- **Default temperature** — 0 deterministic, 0.7 balanced, 1.2+ creative.
- **Max tokens** — `num_predict` cap. `-1` = unlimited.
- **Default system prompt** — applied when you create a new chat.

## Per-conversation overrides

Click the model chip in the top bar (or the ⋮ icon next to it) to change
the model, temperature, or system prompt for the current chat without
touching your defaults. Pick a preset from the prompt-library button (the
notebook icon).

## Code review (`/codereview`)

Type a slash command in the composer to review code from anywhere:

```
/codereview git@github.com:ndramesh/shivagpt.git
/codereview https://github.com/ndramesh/shivagpt/blob/main/server.py
/codereview -model qwen2.5-coder:7b shiva@kailash:/home/shiva/some-project
/codereview /home/shiva/some-local-folder   focus on error handling
```

How the path is interpreted (first match wins):

1. `git@github.com:owner/repo[.git]` — SSH-style git remote, routed to
   the GitHub fetcher (not literal SSH).
2. `https://github.com/owner/repo[/blob/branch/path | /tree/branch[/sub]]`
   — uses the GitHub Tree API + raw fetches. Honors `GITHUB_TOKEN`.
3. Any other `http(s)://` — fetched as a single text blob (gist raw, etc.).
4. `user@host:/path` — `ssh -o BatchMode=yes` (key-based auth only),
   enumerates with `find`, then batched `cat` in one round trip.
5. Anything else — local path on whatever host is running the proxy
   (i.e. the DGX).

The review streams into the chat with a markdown preamble listing every
file that was bundled. Files are filtered to common source/text
extensions and the usual junk dirs (`node_modules`, `__pycache__`,
`.git`, `venv`, `dist`, …) are skipped. Caps default to **30 files** and
**120 000 characters**; tune via env vars below.

**Auth requirement.** The endpoint is gated behind the admin token
(same one `/api/state` uses) because it can read arbitrary filesystem
paths and exec `ssh`. Run `/login` once and the token sticks in
localStorage. The frontend will toast *"Admin login required"* if you
forgot.

Env vars (set in `install-service.sh` or `sudo systemctl edit shivagpt`):

- `CODEREVIEW_DEFAULT_MODEL` — used when `-model` isn't passed. Default
  `deepseek-coder-v2`. Override to whatever you've pulled on Ollama.
- `CODEREVIEW_MAX_FILES` — cap on files bundled into one review. Default 30.
- `CODEREVIEW_MAX_CHARS` — total character budget. Default 120 000.
- `GITHUB_TOKEN` — optional; bumps GitHub rate limits and unlocks
  private repos.
- `ADMIN_PASSWORD` — what `/api/login` checks. Default is `ndr123`, so
  **set this** before exposing the service to the network.

If you use the `user@host:/path` form: the systemd unit ships with
`ProtectHome=read-only`, which means `ssh` inside the service can read
keys but can't write `known_hosts`. Pre-seed any hosts you'll review
from once:

```bash
ssh kailash 'ssh-keyscan -H your-other-box >> ~/.ssh/known_hosts'
```

## Streaming behavior

Tokens append as they arrive. If the first token takes more than 4.5
seconds (typical when a model is being loaded into VRAM), a friendly banner
appears so you know it's not stuck. Press **Esc** or click the stop button
to abort generation cleanly.

## Errors

The UI translates the most common failure modes into plain English:

- "Could not reach the ShivaGPT server." → server is down or the API base
  URL is wrong.
- "The DGX server is up but Ollama is not responding." → run `ollama serve`.
- "The model timed out." → likely still loading; retry shortly.

For deeper diagnostics, hit `http://kailash:8000/healthz` and
`http://kailash:8000/api/debug`. Live tail: `ssh kailash 'sudo
journalctl -u shivagpt -f'`.

## File attachments

Click the paperclip in the composer (or drop / paste files into it). The
upload chip shows a progress bar while bytes transfer, then a pulsing bar
during server-side `extracting…`, then the result (page count, row count,
or thumbnail). On error the chip shows a Retry (↻) button — re-uploads
the same file without re-picking it.

- **PDF**: text extracted via `pypdf` (capped at 200 pages, 200 000 chars).
- **CSV**: re-rendered as an aligned plaintext table (capped at 1 000 rows).
- **TXT/MD/JSON/YAML/XML/log**: passed through.
- **PNG/JPG/WebP/GIF**: sent as raw base64 in Ollama's `messages[].images[]`.
  Conversation auto-switches to the configured vision model
  (`qwen2.5vl` by default).

50 MB cap per file. 60 s server-side processing cap. 12 s no-progress
client-side stall detection (so an unreachable server fails fast instead
of leaving the chip at 0%).

## Hacking on the frontend (fast loop)

The static `frontend/index.html` is served fresh on every request — no
build step, no bundler, no caching headers. So any of these workflows
work:

**Run the server on your Mac, talking to remote Ollama** (recommended for UI work):

```bash
./dev.sh
# then open http://localhost:8000 in Safari
```

`dev.sh` boots `uvicorn --reload` against `http://kailash:11434`. Edit
`frontend/index.html`, hit **Cmd-Shift-R** in Safari, see the change.
For this to work, Ollama on the DGX must be reachable on the LAN — start
it with `OLLAMA_HOST=0.0.0.0:11434 ollama serve` (or set the env var in
the ollama systemd unit).

**Edit on Mac, deploy to DGX, refresh:**

```bash
./deploy.sh --service && open -a Safari http://kailash:8000
# (then Cmd-Shift-R after each future edit + ./deploy.sh --service)
```

**Open the file directly in Safari** (no Mac server needed):

```bash
open -a Safari frontend/index.html
# In the app's Settings (gear icon), set API base URL to http://kailash:8000
```

CORS is enabled on the server so any origin works.

## File layout

```
shivagpt/
├── server.py              # FastAPI proxy + static server
├── requirements.txt
├── run.sh                 # one-shot launcher (used on the DGX)
├── dev.sh                 # local dev server on your Mac, proxying to remote Ollama
├── deploy.sh              # rsync-to-DGX + setup venv (--service to install systemd unit)
├── install-service.sh     # writes/enables /etc/systemd/system/shivagpt.service (run as root)
├── README.md
├── CHANGELOG.md
└── frontend/
    └── index.html         # the entire SPA, single file
```

No build step. To tweak the UI, edit `frontend/index.html` and reload
Safari (no need to restart the server).
