# Changelog

All notable changes to ShivaGPT are documented in this file.

## [Unreleased]

### Added
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
