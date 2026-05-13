# Changelog

All notable changes to ShivaGPT are documented in this file.

## [Unreleased]

### Added
- **Scheduled tasks can email their output.** New SMTP delivery layer
  using stdlib `smtplib` + `markdown` for HTML rendering. Configure
  via `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASS`,
  `SMTP_FROM`, `SMTP_USE_TLS` (default 1), `EMAIL_TO_DEFAULT`.
  `POST /api/email/test` sends a small test message so you can verify
  credentials. `/schedule add ... -email me@example.com` attaches an
  email destination to any schedule; output is rendered to nicely-styled
  HTML and delivered after each successful run.
- **Three new scheduler recipes for market briefs:**
    - `morning_brief TICKERS` — composite: portfolio + pre-market
      movers + watchlist quotes + top news, in one email-ready brief.
    - `premarket TICKERS` — Alpaca-backed pre/post-market snapshot
      flagging tickers that moved ≥0.5% from prev close.
    - `top_news TICKERS` — aggregates recent Yahoo headlines across the
      tickers you pass (or defaults to broad indices SPY/QQQ/^GSPC).

### Added
- **RAG knowledge bases (`/kb` + `/ask`).** Drop a folder of docs into
  a named knowledge base; the server chunks them paragraph-aware
  (~1000 chars with 150 char overlap), embeds via Ollama's
  `nomic-embed-text`, and stores the float32 vectors as SQLite BLOBs at
  `data/kb.db`. `/kb list`, `/kb ingest NAME PATH`, `/kb delete NAME`,
  `/kb new NAME`. Then `/ask -kb NAME your question` does brute-force
  cosine top-k retrieval (fast at <100k chunks, no external vector
  store) and streams a cited answer through Ollama. Supports
  `.txt/.md/code`, PDFs (via pypdf), and HTML (via trafilatura).
  New endpoints: `POST /api/kb/{create,ingest,search}`,
  `GET /api/kb/list`, `DELETE /api/kb/{name}`, `POST /api/ask`. Env
  knobs: `KB_DB_PATH`, `KB_EMBED_MODEL` (default `nomic-embed-text`),
  `KB_CHUNK_CHARS`, `KB_CHUNK_OVERLAP`, `KB_DEFAULT_TOP_K`.

- **Scheduled tasks (`/schedule`).** In-process scheduler (no
  APScheduler dep): an asyncio loop ticks every 30s, picks up due
  jobs, runs them through "recipes." Stored in `data/schedules.db`
  alongside a `task_runs` log. Schedule syntax: `daily HH:MM`,
  `weekday HH:MM`, `weekend HH:MM`, `every Nm`, `every Nh`. Recipes:
  `portfolio` (Alpaca account brief), `watchlist TICKER,TICKER,...`,
  `stock TICKER`, `ask KB|QUESTION`. `/schedule add NAME "WHEN"
  RECIPE [args]`, `/schedule list`, `/schedule run ID`,
  `/schedule view ID` (last 5 runs), `/schedule on|off ID`,
  `/schedule delete ID`. Endpoints: `GET/POST /api/schedules`,
  `DELETE /api/schedules/{id}`, `POST /api/schedules/{id}/{run,toggle}`,
  `GET /api/schedules/{id}/runs`.

### Fixed
- **`/imgen` and `/api/transcribe` hit EROFS on first model download.**
  Root cause: the systemd unit shipped with `ProtectHome=read-only` and
  `ReadWritePaths=$SCRIPT_DIR`, which made `~/.cache/huggingface`
  read-only — so diffusers/transformers/faster-whisper couldn't write
  weights on first fetch. `install-service.sh` now adds both
  `~/.cache/huggingface` and `~/.ssh` (the latter unblocks
  `StrictHostKeyChecking=accept-new` for `/codereview`'s SSH path form)
  to `ReadWritePaths`, and pre-creates the directories so systemd has
  something to mount.

### Added
- **Server-backed prompt history with up/down arrow nav.** New SQLite
  database at `data/history.db` records every prompt you send. The
  composer recalls them with **↑** (older) and **↓** (newer), bash
  style. Lives on the server so it follows you across browsers and
  devices (Mac Safari + iPhone share the same history). Up only fires
  when the cursor is on the first row of the composer; down only fires
  while you're already navigating, so it doesn't interfere with editing
  multi-line prompts. **Esc** during nav restores whatever draft you
  were typing. New `/history` slash command lists recent prompts;
  `/history search foo` filters; `/history clear` wipes the database.
  Endpoints: `POST /api/history`, `GET /api/history?q=&limit=`,
  `DELETE /api/history/{id}`, `DELETE /api/history`. Soft cap of
  10 000 rows; oldest are trimmed past that. Env knobs:
  `HISTORY_DB_PATH`, `HISTORY_MAX_ROWS`, `HISTORY_MAX_TEXT_LEN`.
  Admin-gated by the existing `_check_auth`.

### Added
- **Inline Chart.js price charts in `/stock` and `/watchlist`.** The
  1-month closing-price sparkline that `/stock` already returned is now
  rendered as an actual line chart inside the message, color-coded by
  net direction (green up, red down) with a tooltip on hover. Chart.js
  loads from CDN; charts persist on the message and re-render after
  reload. Same chart blocks appear per ticker in `/watchlist`.
- **`/watchlist` slash command.** Track a list of tickers in
  `localStorage`. Sub-commands: `/watchlist add NVDA AAPL TSLA`,
  `/watchlist remove SYM`, `/watchlist clear`, `/watchlist` alone to
  fetch quotes for everything in parallel and render a compact
  dashboard with per-ticker trend charts. Pairs with `/stock <TICKER>`
  for drill-downs.

### Added
- **`/tp` — ThinkOrSwim / Schwab portfolio (read-only).** New endpoint
  `POST /api/tp` reads your Schwab account (ToS got folded into Schwab
  in 2020) via the official Schwab Developer API. Returns balances
  (cash, equity, liquidation value, buying power, day-trading BP) and
  positions (symbol, side, qty, avg price, mkt value, day P&L, open
  P&L) for every linked account. Strictly read-only — only
  `get_account_numbers()` and `get_account(..., fields=POSITIONS)` are
  called. Frontend `/tp` slash command renders a per-account
  dashboard. Requires a one-time OAuth setup
  (`scripts/schwab_auth.py`) and `SCHWAB_APP_KEY` + `SCHWAB_APP_SECRET`
  env vars; README has the full walkthrough.

### Added
- **Voice input via faster-whisper.** A mic button in the composer
  records audio (MediaRecorder → webm/opus), posts it to a new
  `POST /api/transcribe` endpoint, which runs `faster-whisper`
  (default `medium.en`, CPU int8) and returns the text. The result is
  appended into the composer textarea so the user can review/edit
  before sending. Click to start, click to stop. Env knobs:
  `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`.
- **Voice output via Piper TTS.** Every assistant message gets a "Read"
  button that hits `POST /api/tts` with the text (markdown stripped,
  `<think>` blocks excluded), and plays the synthesized audio inline.
  Click again to stop. Uses Piper's Python API for low per-call
  overhead. Env knobs: `PIPER_VOICE` (default
  `/home/shiva/services/piper-voices/en_US-amy-medium.onnx`),
  `TTS_MAX_CHARS` (5000).
- **`/portfolio` slash command + `POST /api/portfolio` endpoint.**
  Pulls account balances (cash, equity, buying power, today's P&L)
  and open positions (symbol, qty, avg entry, current price, market
  value, unrealized P&L, day P&L) from your Alpaca account using
  `TradingClient.get_account()` + `get_all_positions()` — both
  strictly read-only, no order endpoints are wired anywhere on this
  server. Positions table sorts by market value descending.
- **Regenerate keeps alternative branches.** Clicking Regenerate no
  longer clobbers the previous response; it stashes it as an
  alternative and a small `◀ 1/N ▶` widget appears in the message
  header so you can switch between branches. Each branch retains its
  own token counts and timing.

### Added
- **Stock market data layer (Alpaca + yfinance hybrid).** Real-time
  quotes/bars from Alpaca (IEX feed on the free tier), supplemented with
  fundamentals / analyst consensus / news from yfinance (which Alpaca
  doesn't expose). Both run in parallel; each gracefully degrades if
  the other is unavailable. Set `APCA_API_KEY_ID` and
  `APCA_API_SECRET_KEY` in the systemd unit to enable real-time;
  without them everything falls back to yfinance (delayed ~15 min).
  Three new endpoints:
    - `POST /api/stock/quote` — current price, daily change, day/52-week
      range, market cap, P/E, EPS, dividend, beta, sector/industry, a
      brief company summary, and a 1-month closing-price sparkline.
    - `POST /api/stock/analysis` — hand-rolled RSI(14), MACD(12,26,9),
      SMA-20/50/200, Bollinger Bands(20, 2σ). Each indicator is
      translated into a textbook *reading* (overbought / uptrend / etc.)
      with a short note about what traders traditionally take from it.
      Plus analyst-consensus aggregate from Yahoo (mean rating, price
      targets, Strong-Buy / Buy / Hold / Sell / Strong-Sell breakdown)
      and recent news headlines.
    - `POST /api/stock/options` — options chain near at-the-money with
      bid/ask, volume, open interest, implied volatility. Also computes
      payoff math (breakeven, max profit, max loss, premium) for a few
      common strategies: covered call, cash-secured put, ATM bull-call
      spread. Strategy math, not strategy recommendations.
- **`/stock <ticker> [options]` slash command.** Fires the quote +
  analysis (and options if requested) in parallel and renders a single
  dashboard view with header card, fundamentals table, technical
  readings, analyst consensus, options chain, strategy payoffs, and
  recent news. Every panel labels its source; nothing the model "thinks"
  is presented as a recommendation. Run again to refresh.
- Requirements: `yfinance>=0.2.40`, `pandas>=2.0`.

### Changed
- **`/imgen`: aspect ratio accepts `WxH` or `W:H`.** Earlier the parser
  only honored `:` and silently fell back to a square when given `x` —
  fixed so both work, with whitespace tolerated.
- **`/imgen`: community SDXL fine-tunes added.** Vanilla SDXL Base and
  FLUX-schnell produce generic-stylized faces. Two ungated fine-tunes
  are now in the model registry, dramatically better at photorealistic
  faces, anatomy, and hands:
    - `realvis-xl` (aliases: `realvis`, `real`, `realistic`) —
      `SG161222/RealVisXL_V5.0`, currently the strongest free realism
      fine-tune. Best for portraits and people.
    - `juggernaut-xl` (aliases: `juggernaut`, `jug`) —
      `RunDiffusion/Juggernaut-XL-v9`, great general-purpose realism
      and composition.
  Both auto-download via diffusers on first use (~6.5 GB each).

### Changed
- **`/imgen` rebuilt around FLUX + an upscale chain.** SDXL Base is now
  the legacy fallback; the new default is `flux-schnell` (Apache 2.0,
  4-step, ~2–4 s at 1024 on Blackwell). `flux-dev` is available for
  best quality (gated on HF — set `HF_TOKEN`). SDXL still works via
  `-model sdxl`. New flags: `-size N` (square) or `-size WxH`,
  `-aspect 16:9`, `-upscale N` (chains Upscayl/remacri 2×/4× passes,
  capped at 16× for a ~16K final), `-steps`, `-seed`, `-guidance`.
  The endpoint returns native + final dimensions and per-phase timing.
  Env knobs: `IMGEN_DEFAULT_MODEL`, `IMGEN_MAX_OUTPUT_SIDE` (default
  16384), `IMGEN_TIMEOUT_S`. New `GET /api/imgen/models` lists what's
  available with their per-model max native side and default steps.

### Added
- **DeepSeek-R1 / QwQ-style `<think>` block UI** — reasoning models that
  emit a `<think>…</think>` block before the answer now render that block
  as a collapsible widget at the top of the assistant message
  ("💭 Thought for 8s ▾"), expanded while streaming and collapsed when
  done. `<think>` content is stripped from history before sending the
  next turn to Ollama so it doesn't keep eating context.
- **Conversation search match previews** — typing in the sidebar search
  box now shows a snippet of the matching message under each conversation
  title, with the matched substring highlighted. (The filter itself was
  already there; this surfaces *why* each result matched.)
- **Edit and resend user messages** — every user message gets an "Edit"
  button. Clicking it loads the text back into the composer and discards
  the message + everything after it (with a confirm prompt), so typos
  and reframes don't require starting a new conversation.

### Added
- **`/search` slash command** — web search backed by SearXNG running on
  the same host. Streams a cited answer that grounds the model in
  fresh web results. Fetches the top-N pages' main article text (via
  `trafilatura` with a tag-strip fallback) so the model has real
  content to cite from, not just snippets. Optional `-model X` flag,
  optional `-- trailing instructions` after the query.
- **`/fetch` slash command** — pull a single URL, extract main text,
  and stream a model response that uses it as context. Useful for
  "summarize this page" / "what does this say about Y" workflows.
- **`POST /api/search` and `POST /api/fetch` endpoints** — auth-gated
  via the existing admin token. Talk to a local SearXNG at
  `SEARXNG_URL` (default `http://localhost:8888`). Knobs:
  `SEARCH_DEFAULT_RESULTS` (6), `SEARCH_DEFAULT_FETCH` (3),
  `SEARCH_FETCH_MAX_CHARS` (8 000 per page),
  `FETCH_MAX_CHARS` (40 000 for `/api/fetch`),
  `FETCH_TIMEOUT_S` (15), `SEARCH_DEFAULT_MODEL` (`llama3.3`).
- **Generic `streamFromEndpoint()` frontend helper** — single shared
  NDJSON streamer used by `/search` and `/fetch`. `streamChat` and
  `streamCodeReview` remain as-is for now to avoid churn; future
  commands should use the generic helper.

- **`/codereview` slash command** — review code from a GitHub URL, an
  ssh-style git remote (`git@github.com:owner/repo[.git]`), any other
  `http(s)://` URL, an SSH filesystem path (`user@host:/path`), or a
  local path on the DGX. Optional `-model <name>` flag overrides the
  default. Streams the review into the chat like a normal assistant reply
  with a preamble listing every file that was bundled.
- **`POST /api/codereview` endpoint** — auth-gated through the existing
  admin token (`_check_auth`, same as `/api/state`) because it can read
  arbitrary filesystem paths and shell out to `ssh`. Body:
  `{model?, path, instructions?, temperature?}`. Streams NDJSON in the
  same shape as `/api/chat` so the frontend reader is reused. Tunable via
  env: `CODEREVIEW_DEFAULT_MODEL` (default `deepseek-coder-v2`),
  `CODEREVIEW_MAX_FILES` (30), `CODEREVIEW_MAX_CHARS` (120 000). Honors
  `GITHUB_TOKEN` for higher GitHub API rate limits / private repos. SSH
  enumeration uses one round-trip `find` and a batched marker-delimited
  `cat`; only key-based auth is allowed (`BatchMode=yes`).

### Fixed
- **iPhone portrait blank screen** — The mobile CSS grid gave the sidebar a
  `0`-width column and made it `position: fixed` (out of flow), which caused
  `.main` to auto-place into that zero-width column instead of the `1fr`
  column. Changed mobile grid to `grid-template-columns: 1fr` since the
  fixed-positioned sidebar doesn't need a grid slot.
- **iOS Safari viewport height** — Added `-webkit-fill-available` and a
  `visualViewport` resize handler so the app fills the actual visible area
  on iOS Safari, where `100vh` / `100dvh` include space behind the dynamic
  address bar and home indicator.
- **iPhone safe-area insets** — Added `env(safe-area-inset-*)` padding to the
  topbar and composer so content isn't obscured by the notch or home bar on
  modern iPhones.
- **Keyboard hides composer on iPhone** — When the iOS keyboard opens, the
  `visualViewport` resize handler now scrolls the composer into view.  A
  `focus` listener on the textarea also triggers a scroll after the keyboard
  animation completes.
- **Enter / send button not working on iPhone** — iOS Safari's predictive
  text sets `isComposing = true` on the Enter key, blocking the send handler.
  Switched to explicit `compositionstart`/`compositionend` tracking.  Also
  added a `touchend` listener on the send button since `click` can be
  unreliable during viewport reflow on iOS.
  filling 0→100% during the network upload (XHR-based for upload events),
  then a pulsing bar during server-side `extracting…`. Status text now
  reads `uploading 45% · 1.2 MB / 2.6 MB` then `extracting… 3s`.
- **Verbose debug flag** — `SHIVAGPT_DEBUG=1` env var (or `--debug` CLI
  flag) bumps logging to DEBUG, installs a per-request access log, and
  logs Ollama chat-call summaries (model, message count, image count,
  content size, last user-message preview). New `GET /api/debug` endpoint
  returns the active config so you can confirm verbose mode is live.
- **Service ships with debug ON** by default. Disable later with
  `sudo systemctl edit shivagpt` and add `Environment=SHIVAGPT_DEBUG=0`.

### Added
- **File attachments** — paperclip in the composer accepts PDF, CSV, PNG,
  JPG, WebP, GIF, TXT, MD, JSON, and other text files. Drop files into the
  composer or paste images directly. Up to 50MB per file.
- **Document Q&A** — PDFs are extracted via `pypdf` (capped at 200 pages
  / 200k chars), CSVs are pretty-printed as aligned tables, plain text
  files pass through. Extracted content is folded into the user message
  as context with `--- Attached file: NAME ---` markers.
- **Image Q&A (vision)** — Images are sent as base64 in Ollama's
  `messages[].images[]` field. The conversation auto-switches to a
  vision-capable model (default `qwen2.5vl`) when an image is attached;
  the original model can be restored manually via the model chip.
- **`POST /api/files` endpoint** — multipart upload returning extracted
  text or base64 image, depending on file type.
- **`visionModel` setting** — defaults to `qwen2.5vl`. Used as the
  auto-switch target when an image is attached.

### Fixed
- **Enter key on iPhone** — added `enterkeyhint="send"` and a
  `beforeinput` listener for `insertLineBreak` (iOS soft keyboard often
  fires `beforeinput` instead of `keydown`).
- **Composer hidden by iPhone keyboard** — replaced the JS-based
  `position: fixed` workaround with `interactive-widget=resizes-content`
  in the viewport meta. iOS 16.4+ now resizes the layout viewport itself,
  so the existing flex layout naturally pushes the composer above the
  keyboard with no extra scaffolding.

### Fixed
- **iPhone portrait blank screen** — The mobile CSS grid gave the sidebar a
  `0`-width column and made it `position: fixed` (out of flow), which caused
  `.main` to auto-place into that zero-width column instead of the `1fr`
  column. Changed mobile grid to `grid-template-columns: 1fr` since the
  fixed-positioned sidebar doesn't need a grid slot.
- **iOS Safari viewport height** — Added `-webkit-fill-available` and `100dvh`
  fallbacks so the app fills the actual visible area on iOS Safari, where
  `100vh` includes space behind the dynamic address bar.
- **iPhone safe-area insets** — Added `env(safe-area-inset-*)` padding to the
  topbar and composer so content isn't obscured by the notch or home bar on
  modern iPhones.
- **Keyboard hides composer on iPhone** — On iOS Safari, the virtual keyboard
  overlays the page without resizing the layout viewport, hiding the composer.
  Now uses `visualViewport` to detect the keyboard and floats the composer
  (`position: fixed`) above it via a `--keyboard-offset` CSS variable.
  The `.keyboard-open` class is toggled on focus/blur and viewport resize.
- **Enter / send button not working on iPhone** — iOS Safari's predictive
  text sets `isComposing = true` on the Enter key, blocking the send handler.
  Switched to explicit `compositionstart`/`compositionend` tracking.  Also
  added a `touchend` listener on the send button since `click` can be
  unreliable during viewport reflow on iOS.
