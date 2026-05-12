# Changelog

All notable changes to ShivaGPT are documented in this file.

## [Unreleased]

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
