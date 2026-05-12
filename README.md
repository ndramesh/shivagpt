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

## Web search (`/search`) and URL fetch (`/fetch`)

`/search` runs a query through a local SearXNG instance, pulls full
text from the top results, and streams a cited answer:

```
/search current best free image-gen models
/search -model llama3.3 weather in Mountain View tomorrow -- one line only
/fetch https://news.ycombinator.com/item?id=12345
/fetch https://en.wikipedia.org/wiki/Stuff   summarize in 5 bullets
```

The answer is prefaced with a collapsible "Search results" block that
shows every URL the model was given access to, and the model is
prompted to cite claims as `[1]`, `[2]`, etc. matching that list.

You need SearXNG running locally (see the install commands in the
project notes). Env vars on the ShivaGPT server side:

- `SEARXNG_URL` — default `http://localhost:8888`.
- `SEARCH_DEFAULT_MODEL` — default `llama3.3`.
- `SEARCH_DEFAULT_RESULTS` — how many results to surface (6).
- `SEARCH_DEFAULT_FETCH` — how many top results to pull full text from (3).
- `SEARCH_FETCH_MAX_CHARS` — per-page text cap (8 000).
- `FETCH_MAX_CHARS` — single-page cap for `/api/fetch` (40 000).
- `FETCH_TIMEOUT_S` — per-URL fetch timeout (15 s).

Like `/codereview`, both endpoints require an admin login (`/login`).

## Image generation (`/imgen`)

`/imgen` produces images via FLUX or SDXL on the DGX, with an optional
AI-upscale chain (Upscayl/remacri) for reaching HD/4K/8K/16K outputs:

```
/imgen a koi pond in late afternoon light
/imgen -size 2048 -aspect 16:9 cinematic mountain sunrise
/imgen -upscale 4 -size 2048 modernist house in a redwood grove   # → 8K
/imgen -model flux-dev -steps 25 -size 1536 close-up of a hummingbird
/imgen -model sdxl -seed 12345 a watercolor coffee shop interior
```

Flags (all optional, any order, then prompt at the end):

- `-model <name>` — supported models, listed in rough quality order
  for realistic content:
    - `realvis-xl` (aliases: `realvis`, `real`, `realistic`) —
      community SDXL fine-tune by SG161222. **Best free option for
      photorealistic faces and people.** Ungated.
    - `juggernaut-xl` (alias: `jug`) — another community SDXL fine-tune,
      great general-purpose realism. Ungated.
    - `flux-dev` — Black Forest Labs FLUX.1-dev, gated on HF (needs
      `HF_TOKEN`). Excellent prompt adherence, slightly stylized look.
    - `flux-schnell` (alias: `schnell`, default) — fast 4-step FLUX,
      Apache 2.0, ungated. Good for quick iteration, weaker on faces.
    - `sdxl` — legacy SDXL Base, kept for compatibility. Use one of the
      community fine-tunes above instead.
- `-size N` — square N×N. Or `-size WxH` for explicit aspect.
- `-aspect W:H` — used with `-size N` to derive non-square.
- `-upscale N` — chain Upscayl 2× / 4× passes after generation to
  reach N× the native side (capped at 16, so 2048 native + 8× = 16K).
- `-steps N` — diffusion steps (defaults: schnell 4, dev 20, sdxl 25).
- `-guidance N` — CFG scale (no effect on schnell, which is distilled).
- `-seed N` — reproducibility.

The model is sized to native at the GPU's sweet spot (FLUX up to 2048,
SDXL up to 1536), then upscaled in 2× or 4× passes by Upscayl. Past
~8K the upscaler is doing most of the work — useful for prints, less so
for visible-on-screen detail.

Env knobs:

- `IMGEN_DEFAULT_MODEL` — default `flux-schnell`.
- `IMGEN_MAX_OUTPUT_SIDE` — final-image side cap, default `16384`.
- `IMGEN_TIMEOUT_S` — per-call timeout, default 600 s.
- `HF_TOKEN` — required for FLUX.1-dev (gated repo on Hugging Face).

`GET /api/imgen/models` returns the registry with per-model `max_native`
and `default_steps`, for building a UI picker.

## Stock market (`/stock`)

Type a ticker and get a complete research dashboard rendered into the chat:

```
/stock NVDA
/stock AAPL options
/stock TSLA news
```

The view bundles in one response: current quote with daily change,
fundamentals table (market cap, P/E, EPS, dividend, beta, 52-week
range), hand-computed technical indicators (RSI, MACD, SMA-20/50/200,
Bollinger) with textbook readings of each, analyst consensus
aggregated from Yahoo Finance (Strong-Buy / Buy / Hold / Sell /
Strong-Sell breakdown + price targets), and recent headlines. With
`options` appended you also get the chain near at-the-money plus
strategy math (covered call, cash-secured put, vertical spread) — the
numbers, not the recommendation.

**This is research data, not financial advice.** No buy/sell calls are
generated by the model; ratings are third-party Wall Street aggregates,
and strategy entries show breakeven / max profit / max loss so you can
evaluate the trade yourself.

### Data sources

`/stock` uses two sources in parallel and merges them:

- **Alpaca** (primary, real-time IEX feed) — current price, bid/ask,
  daily/intraday bars used for technical indicators. Requires API keys
  in env (free tier is fine, paper or live both work for *data*):

  ```bash
  ssh kailash 'sudo systemctl edit shivagpt'
  # add:
  [Service]
  Environment=APCA_API_KEY_ID=PKxxxxxxxxxx
  Environment=APCA_API_SECRET_KEY=xxxxxxxxxxxxxxx
  # save, then:
  ssh kailash 'sudo systemctl restart shivagpt'
  ```

- **yfinance** (supplemental) — fundamentals (P/E, EPS, market cap,
  beta, sector, summary), analyst consensus, recent news headlines.
  Alpaca doesn't expose these. No API key, ~15 min delayed.

If Alpaca keys aren't set, everything falls back to yfinance and the
header shows "delayed ~15 min" — still works, just not real-time.

Server endpoints (all unauth'd):

- `POST /api/stock/quote` — `{ticker, period?}`
- `POST /api/stock/analysis` — `{ticker, period_days?}`
- `POST /api/stock/options` — `{ticker, expiry?, strikes_each_side?}`

The options chain currently still comes from yfinance (it's a simpler
API surface than Alpaca's contract-discovery flow). If you need
real-time options quotes too, the Alpaca `OptionHistoricalDataClient`
helpers are already wired up in `server.py` and can be plugged in.

## Voice in (mic button) and voice out (read aloud)

The composer has a microphone button next to the paperclip. Click to
start recording (the icon pulses red), click again to stop. The audio
is uploaded to `POST /api/transcribe`, which runs `faster-whisper`
(default `medium.en` on Grace CPU, ~5-10× realtime), and the
transcribed text is inserted into the composer so you can review and
edit before sending. Browser permission for the microphone is
required on first use.

Every assistant message gets a **Read** button next to Copy and
Regenerate. Clicking it hits `POST /api/tts` with the message text
(markdown stripped, `<think>` reasoning excluded) and plays the
generated audio inline using Piper TTS. Click again to stop.

Env knobs (in the systemd unit):

- `WHISPER_MODEL` — default `medium.en`. Try `large-v3` for top
  accuracy at the cost of speed.
- `WHISPER_DEVICE` — `cpu` (default) or `cuda` if you set up a
  CUDA-enabled ctranslate2.
- `PIPER_VOICE` — absolute path to a `.onnx` Piper voice. Default
  expects `~/services/piper-voices/en_US-amy-medium.onnx`.

## Portfolio (`/portfolio`)

`/portfolio` shows your Alpaca account at a glance: cash, equity,
buying power, day P&L, plus every open position with its avg entry,
current price, market value, unrealized P&L, and day P&L — sorted by
market value descending. Click a ticker mention in the table notes to
drill into `/stock <symbol>`.

This endpoint (`POST /api/portfolio`) is strictly read-only: the
server calls `TradingClient.get_account()` and `get_all_positions()`
and nothing else. There are no order-submission endpoints anywhere on
this server. Requires `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` in
the systemd unit (same keys used by `/stock`).

## ThinkOrSwim / Schwab (`/tp`)

`/tp` shows your Schwab/ThinkOrSwim account balances and positions
(read-only). Schwab acquired TD Ameritrade in 2020, so the
ThinkOrSwim account you log into is now a Schwab account — same
positions, same money, accessed via the Schwab Developer API.

### One-time setup

The Schwab API uses OAuth2, which is fiddlier than a static API key.
You only do this once.

**1. Register a developer app** at
[developer.schwab.com](https://developer.schwab.com):

- Create a free personal account (use the same login as your trading
  account).
- Dashboard → API Products → **enable both**: "Accounts and Trading
  Production" and "Market Data Production" (both free).
- Dashboard → Add a New App. Fields:
    - **App Name**: anything (e.g. `shivagpt-personal`)
    - **Callback URL**: `https://127.0.0.1` (exactly that, no port)
    - **API Product**: select both products from above
- Submit. Schwab reviews new apps; **approval usually arrives within
  a day**, often hours. You can't authenticate until it shows
  "Approved." (You'll get an email.)
- Once approved, open the app and copy "App Key" and "App Secret."

**2. Set the credentials** in the systemd unit:

```bash
ssh kailash 'sudo systemctl edit shivagpt'
# add:
[Service]
Environment=SCHWAB_APP_KEY=your_app_key_here
Environment=SCHWAB_APP_SECRET=your_app_secret_here
# save, then:
ssh kailash 'sudo systemctl restart shivagpt'
```

**3. Run the one-time OAuth flow.** This generates the token file
that the server uses thereafter. Easiest is on your Mac (browser
available):

```bash
cd ~/src/shivagpt
export SCHWAB_APP_KEY=your_app_key_here
export SCHWAB_APP_SECRET=your_app_secret_here
./scripts/schwab_auth.py --token-path data/schwab-token.json
```

A browser tab opens to the Schwab login page. Log in, allow access,
and the script captures the redirect automatically. When it prints
"OK — Schwab returned N account(s)," copy the token to kailash:

```bash
scp data/schwab-token.json kailash:~/shivagpt/data/schwab-token.json
ssh kailash 'sudo systemctl restart shivagpt'
```

If you'd rather do everything on kailash (no Mac browser), use the
manual flow — the script prints an auth URL, you open it in any
browser, log in, and paste the redirected URL back:

```bash
ssh kailash 'cd ~/shivagpt && \
  SCHWAB_APP_KEY=... SCHWAB_APP_SECRET=... \
  .venv/bin/python scripts/schwab_auth.py --manual'
```

**4. Try it.** Open Safari at `http://kailash:8000`, hard reload,
and type `/tp`. You should see your account balances and positions.

### Token lifetime

Schwab's OAuth refresh tokens last **7 days**. The server refreshes
the access token automatically on every API call (so you can use
`/tp` continuously for a week without re-auth). If you go more than
7 days between uses, the refresh token expires and you need to re-run
`scripts/schwab_auth.py` to mint a new one. The script overwrites
the existing token file in place — no other changes needed.

### What it can't do

Place trades. The server only calls Schwab's `get_account_numbers()`
and `get_account(..., fields=POSITIONS)`. There are no order
endpoints wired anywhere on this server.

## Regenerate keeps branches

Hit **Regenerate** on an assistant message and the previous response
isn't lost — it's stashed as an alternative branch. A `◀ 1/N ▶`
widget appears in the message header; use the arrows to switch
between branches without re-running anything. Each branch retains
its own token counts, timing, and `<think>` reasoning.

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
