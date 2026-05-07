"""
ShivaGPT backend.

Tiny FastAPI server that:
  1. Serves the single-page frontend at /
  2. Proxies /api/* to the local Ollama daemon (same host, default :11434)
  3. Streams /api/chat responses back to the browser as NDJSON

Run on the DGX:
    python3 server.py --host 0.0.0.0 --port 8000

Then open http://kailash:8000 on your Mac.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import csv as csv_module
import io
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

def _is_truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in {"1", "true", "yes", "on", "y", "t"}


DEBUG = _is_truthy(os.getenv("SHIVAGPT_DEBUG"))

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
)
# httpx is chatty at DEBUG; keep it at INFO unless the operator really wants it.
logging.getLogger("httpx").setLevel(logging.DEBUG if DEBUG else logging.WARNING)

log = logging.getLogger("shivagpt")
if DEBUG:
    log.info("Verbose debugging ENABLED (SHIVAGPT_DEBUG=1)")
else:
    log.info("Set SHIVAGPT_DEBUG=1 in the environment for verbose logs")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
FRONTEND_DIR = Path(__file__).parent / "frontend"

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
ADMIN_TOKENS: set[str] = set()

app = FastAPI(title="ShivaGPT", version="1.0.0")

# Permissive CORS so you can develop the frontend locally (open index.html
# from your Mac at file:// or http://localhost) and still hit this server.
# Safe here because the server only proxies a local Ollama on a LAN box.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """Per-request access log. Always on at DEBUG; quiet at INFO."""
    t0 = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as e:
        log.exception("UNHANDLED %s %s", request.method, request.url.path)
        raise
    dt_ms = (time.monotonic() - t0) * 1000
    if DEBUG:
        client = request.client.host if request.client else "?"
        log.debug(
            "%-4s %s -> %d  %.0fms  client=%s  ua=%r",
            request.method, request.url.path, response.status_code, dt_ms,
            client, (request.headers.get("user-agent") or "")[:80],
        )
    return response


@app.get("/api/debug")
async def debug_info() -> dict[str, Any]:
    """Introspection — useful for confirming verbose logging is on."""
    return {
        "debug": DEBUG,
        "ollama_url": OLLAMA_URL,
        "log_level": logging.getLevelName(log.getEffectiveLevel()),
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
        "file_process_timeout_s": FILE_PROCESS_TIMEOUT,
        "max_pdf_pages": MAX_PDF_PAGES,
        "max_text_chars": MAX_TEXT_CHARS,
    }


_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


@app.get("/")
async def root() -> FileResponse:
    # No-cache so a deploy is picked up by the browser on the next reload
    # without needing Cmd-Shift-R every time.
    return FileResponse(FRONTEND_DIR / "index.html", headers=_NO_CACHE)


@app.get("/manifest.webmanifest")
async def manifest() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icon.svg")
async def icon() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "icon.svg", media_type="image/svg+xml")


@app.get("/icon-192.png")
async def icon_192() -> Response:
    # Fallback PNG built lazily from the SVG (Chrome's "install as app" wants
    # at least one raster icon). Generated once per process.
    return _png_icon(192)


@app.get("/icon-512.png")
async def icon_512() -> Response:
    return _png_icon(512)


_PNG_CACHE: dict[int, bytes] = {}


def _png_icon(size: int) -> Response:
    if size in _PNG_CACHE:
        return Response(content=_PNG_CACHE[size], media_type="image/png")
    # Tiny dependency-free PNG: a flat purple square with an "S" glyph would
    # need a font. We instead emit a simple radial-gradient circle PNG using
    # the standard library's tk-free path: hand-rolled with zlib + chunks.
    # For brevity we just emit a solid-color square; the SVG icon is what
    # Safari's "Add to Dock" actually uses.
    import struct, zlib
    w = h = size
    # purple-ish #7c5cff
    r, g, b = 0x7C, 0x5C, 0xFF
    raw = b"".join(b"\x00" + bytes([r, g, b]) * w for _ in range(h))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, 6)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    _PNG_CACHE[size] = png
    return Response(content=png, media_type="image/png")


@app.get("/healthz")
async def healthz() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
            r.raise_for_status()
        return {"ok": True, "ollama": OLLAMA_URL}
    except Exception as e:
        return JSONResponse({"ok": False, "ollama": OLLAMA_URL, "error": str(e)}, status_code=503)


@app.get("/api/models")
async def list_models() -> Response:
    """Return Ollama's model list (passthrough)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(f"{OLLAMA_URL}/api/tags")
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach Ollama at {OLLAMA_URL}: {e}")


@app.post("/api/show")
async def show_model(req: Request) -> Response:
    """Proxy Ollama's /api/show — used by the UI to read each model's
    context length so it can display 'used / total tokens'."""
    body = await req.body()
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(
                f"{OLLAMA_URL}/api/show",
                content=body,
                headers={"content-type": "application/json"},
            )
        return Response(content=r.content, status_code=r.status_code, media_type="application/json")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach Ollama at {OLLAMA_URL}: {e}")


@app.post("/api/chat")
async def chat(req: Request) -> StreamingResponse:
    """Stream a chat completion from Ollama back to the client as NDJSON.

    The browser sends the standard Ollama /api/chat payload; we forward it
    as-is and pipe the streaming response straight through.
    """
    body = await req.body()

    if DEBUG:
        try:
            payload = json.loads(body)
            msgs = payload.get("messages", []) or []
            n_imgs = sum(len(m.get("images", []) or []) for m in msgs if isinstance(m, dict))
            total_chars = sum(len((m.get("content") or "")) for m in msgs if isinstance(m, dict))
            log.debug(
                "chat: model=%s msgs=%d images=%d content_chars=%d temp=%s num_predict=%s",
                payload.get("model"), len(msgs), n_imgs, total_chars,
                (payload.get("options") or {}).get("temperature"),
                (payload.get("options") or {}).get("num_predict"),
            )
            last_user = next((m for m in reversed(msgs)
                              if isinstance(m, dict) and m.get("role") == "user"), None)
            if last_user:
                preview = (last_user.get("content") or "")[:160].replace("\n", " ")
                log.debug("chat: last user msg: %r%s", preview, "…" if len(last_user.get("content") or "") > 160 else "")
        except Exception as e:
            log.debug("chat: could not parse body for logging: %s", e)

    async def streamer():
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream(
                    "POST",
                    f"{OLLAMA_URL}/api/chat",
                    content=body,
                    headers={"content-type": "application/json"},
                ) as r:
                    if r.status_code != 200:
                        text = await r.aread()
                        msg = text.decode("utf-8", errors="replace")
                        yield (json.dumps({"error": msg, "status": r.status_code}) + "\n").encode()
                        return
                    async for chunk in r.aiter_raw():
                        if chunk:
                            yield chunk
        except httpx.ConnectError as e:
            yield (json.dumps({"error": f"Cannot connect to Ollama at {OLLAMA_URL}. Is it running?"}) + "\n").encode()
        except httpx.ReadTimeout:
            yield (json.dumps({"error": "Ollama timed out while generating. Try a smaller prompt or a faster model."}) + "\n").encode()
        except Exception as e:
            log.exception("chat stream failed")
            yield (json.dumps({"error": f"Server error: {e.__class__.__name__}: {e}"}) + "\n").encode()

    return StreamingResponse(
        streamer(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB
MAX_PDF_PAGES    = 200
MAX_TEXT_CHARS   = 200_000            # cap extracted text to keep prompts sane

IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
TEXT_MIMES  = {"text/plain", "text/markdown", "text/csv", "application/json",
               "application/x-yaml", "text/yaml", "application/xml", "text/xml"}
PDF_MIMES   = {"application/pdf"}


def _truncate(s: str, limit: int) -> tuple[str, bool]:
    if len(s) <= limit:
        return s, False
    return s[:limit], True


def _extract_pdf(data: bytes, filename: str) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise HTTPException(status_code=500, detail=f"pypdf not installed: {e}")
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not read PDF: {e}")
    pages = []
    for i, page in enumerate(reader.pages):
        if i >= MAX_PDF_PAGES:
            break
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:
            log.warning("PDF page %d extract failed: %s", i, e)
            pages.append("")
    text = "\n\n".join(f"--- Page {i+1} ---\n{p}" for i, p in enumerate(pages) if p.strip())
    text, truncated = _truncate(text, MAX_TEXT_CHARS)
    return {
        "kind": "text",
        "filename": filename,
        "mime": "application/pdf",
        "pages": len(reader.pages),
        "pages_extracted": len(pages),
        "text": text,
        "truncated": truncated,
        "size": len(data),
    }


def _extract_csv(data: bytes, filename: str) -> dict[str, Any]:
    # Best-effort decode and re-format as a clean table for the LLM
    try:
        text_in = data.decode("utf-8")
    except UnicodeDecodeError:
        text_in = data.decode("latin-1", errors="replace")
    rows = list(csv_module.reader(io.StringIO(text_in)))
    if not rows:
        body = ""
    else:
        # Render as a simple aligned text table; cap at 1000 rows.
        rows = rows[:1000]
        widths = [max(len(str(r[i])) if i < len(r) else 0 for r in rows)
                  for i in range(max(len(r) for r in rows))]
        lines = []
        for ri, r in enumerate(rows):
            line = "  ".join(str(r[i] if i < len(r) else "").ljust(widths[i]) for i in range(len(widths)))
            lines.append(line.rstrip())
            if ri == 0:
                lines.append("  ".join("-" * w for w in widths))
        body = "\n".join(lines)
    body, truncated = _truncate(body, MAX_TEXT_CHARS)
    return {
        "kind": "text",
        "filename": filename,
        "mime": "text/csv",
        "rows": len(rows),
        "text": body,
        "truncated": truncated,
        "size": len(data),
    }


def _extract_text_file(data: bytes, filename: str, mime: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1", errors="replace")
    text, truncated = _truncate(text, MAX_TEXT_CHARS)
    return {
        "kind": "text",
        "filename": filename,
        "mime": mime,
        "text": text,
        "truncated": truncated,
        "size": len(data),
    }


def _wrap_image(data: bytes, filename: str, mime: str) -> dict[str, Any]:
    return {
        "kind": "image",
        "filename": filename,
        "mime": mime,
        "base64": base64.b64encode(data).decode("ascii"),
        "size": len(data),
    }


FILE_PROCESS_TIMEOUT = 60.0   # seconds; PDF parsing can be CPU-heavy


@app.post("/api/files")
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """Accept a single file upload and return content the chat UI can use:
       text-extracted (for PDF/CSV/TXT/MD/JSON) or base64 (for images).

    Heavy parsing (pypdf) runs in a worker thread so it doesn't block the
    event loop and starve other requests. A timeout caps total time spent
    on any one file."""
    t_start = time.monotonic()
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"File too large (max {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
    mime = (file.content_type or "").lower()
    name = file.filename or "upload"
    suffix = Path(name).suffix.lower()

    # Best-effort mime inference if browser didn't send one
    if not mime:
        mime = {
            ".pdf": "application/pdf", ".csv": "text/csv",
            ".txt": "text/plain", ".md": "text/markdown",
            ".json": "application/json",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".gif": "image/gif",
        }.get(suffix, "application/octet-stream")

    log.info("upload start: %s (%s, %d bytes)", name, mime, len(raw))

    def _do() -> dict[str, Any]:
        if mime in PDF_MIMES or suffix == ".pdf":
            return _extract_pdf(raw, name)
        if mime == "text/csv" or suffix == ".csv":
            return _extract_csv(raw, name)
        if mime in IMAGE_MIMES or suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            return _wrap_image(raw, name, mime)
        if mime in TEXT_MIMES or suffix in {".txt", ".md", ".json", ".yaml", ".yml", ".xml", ".log"}:
            return _extract_text_file(raw, name, mime)
        # Last-ditch attempt: if it decodes as UTF-8, treat as text
        try:
            sample = raw[:4096].decode("utf-8")
            if sample.isprintable() or "\n" in sample:
                return _extract_text_file(raw, name, "text/plain")
        except UnicodeDecodeError:
            pass
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {mime or suffix or 'unknown'}")

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_do), timeout=FILE_PROCESS_TIMEOUT)
    except asyncio.TimeoutError:
        log.warning("upload timeout: %s after %.1fs", name, time.monotonic() - t_start)
        raise HTTPException(
            status_code=504,
            detail=f"File processing timed out after {int(FILE_PROCESS_TIMEOUT)}s. PDF may be very large or contain scanned images.",
        )
    log.info("upload done:  %s in %.2fs", name, time.monotonic() - t_start)
    return result


# ---------------------------------------------------------------------------
# Image manipulation
# ---------------------------------------------------------------------------

VALID_IMAGE_OPS = {
    "upscale", "resize", "crop", "rotate", "flip",
    "brightness", "contrast", "sharpen", "blur",
    "grayscale", "invert",
}


def _process_image(img_bytes: bytes, operation: str, params: dict) -> tuple[bytes, str]:
    """Apply a single image operation and return (png_bytes, mime)."""
    from PIL import Image, ImageEnhance, ImageFilter

    img = Image.open(io.BytesIO(img_bytes))
    # Ensure RGB for most operations (handle RGBA gracefully)
    had_alpha = img.mode == "RGBA"

    if operation == "upscale":
        import subprocess
        upscayl_bin = DATA_DIR / "upscayl" / "resources" / "bin" / "upscayl-bin"
        models_dir = DATA_DIR / "upscayl" / "resources" / "models"
        
        if upscayl_bin.exists() and models_dir.exists():
            log.info("Using Upscayl for AI upscaling...")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f_in, \
                 tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f_out:
                
                img.save(f_in.name, format="PNG")
                
                cmd = [
                    str(upscayl_bin),
                    "-i", f_in.name,
                    "-o", f_out.name,
                    "-s", "2",
                    "-m", str(models_dir),
                    "-n", "remacri"
                ]
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    img = Image.open(f_out.name)
                    img.load()
                except subprocess.CalledProcessError as e:
                    log.error(f"Upscayl failed: {e.stderr.decode()}")
                    raise ValueError("AI Upscaling failed")
                finally:
                    try:
                        os.unlink(f_in.name)
                        os.unlink(f_out.name)
                    except OSError:
                        pass
        else:
            log.warning("Upscayl not found, falling back to PIL Lanczos.")
            factor = int(params.get("factor", 2))
            factor = max(1, min(factor, 8))
            new_size = (img.width * factor, img.height * factor)
            img = img.resize(new_size, Image.LANCZOS)

    elif operation == "resize":
        w = int(params.get("width", img.width))
        h = int(params.get("height", img.height))
        w = max(1, min(w, 16384))
        h = max(1, min(h, 16384))
        img = img.resize((w, h), Image.LANCZOS)

    elif operation == "crop":
        left = int(params.get("left", 0))
        top = int(params.get("top", 0))
        right = int(params.get("right", img.width))
        bottom = int(params.get("bottom", img.height))
        img = img.crop((left, top, right, bottom))

    elif operation == "rotate":
        degrees = float(params.get("degrees", 90))
        expand = params.get("expand", True)
        img = img.rotate(-degrees, expand=expand, resample=Image.LANCZOS)

    elif operation == "flip":
        direction = params.get("direction", "horizontal")
        if direction == "vertical":
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        else:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)

    elif operation == "brightness":
        factor = float(params.get("factor", 1.2))
        factor = max(0.0, min(factor, 5.0))
        img = ImageEnhance.Brightness(img).enhance(factor)

    elif operation == "contrast":
        factor = float(params.get("factor", 1.3))
        factor = max(0.0, min(factor, 5.0))
        img = ImageEnhance.Contrast(img).enhance(factor)

    elif operation == "sharpen":
        strength = int(params.get("strength", 1))
        for _ in range(max(1, min(strength, 5))):
            img = img.filter(ImageFilter.SHARPEN)

    elif operation == "blur":
        radius = float(params.get("radius", 3))
        radius = max(0.5, min(radius, 50))
        img = img.filter(ImageFilter.GaussianBlur(radius=radius))

    elif operation == "grayscale":
        img = img.convert("L").convert("RGBA" if had_alpha else "RGB")

    elif operation == "invert":
        from PIL import ImageOps
        if had_alpha:
            r, g, b, a = img.split()
            rgb = Image.merge("RGB", (r, g, b))
            rgb = ImageOps.invert(rgb)
            img = Image.merge("RGBA", (*rgb.split(), a))
        else:
            if img.mode != "RGB":
                img = img.convert("RGB")
            img = ImageOps.invert(img)

    else:
        raise ValueError(f"Unknown operation: {operation}")

    # Encode result
    out = io.BytesIO()
    fmt = "PNG" if had_alpha else "JPEG"
    mime = "image/png" if had_alpha else "image/jpeg"
    save_kw = {"quality": 92} if fmt == "JPEG" else {}
    if img.mode == "RGBA" and fmt == "JPEG":
        img = img.convert("RGB")
    img.save(out, format=fmt, **save_kw)
    return out.getvalue(), mime, img.width, img.height


@app.post("/api/image")
async def process_image(req: Request) -> dict[str, Any]:
    """Apply an image manipulation operation.

    Accepts JSON: { "image": "<base64>", "operation": "upscale", "params": { "factor": 2 } }
    Returns JSON: { "image": "<base64>", "mime": "image/png", "width": ..., "height": ..., ... }
    """
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    b64_in = body.get("image", "")
    operation = body.get("operation", "").lower().strip()
    params = body.get("params", {}) or {}

    if not b64_in:
        raise HTTPException(status_code=400, detail="Missing 'image' (base64)")
    if operation not in VALID_IMAGE_OPS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid operation '{operation}'. Valid: {', '.join(sorted(VALID_IMAGE_OPS))}",
        )

    try:
        img_bytes = base64.b64decode(b64_in)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    if len(img_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image too large")

    # Get original dimensions
    from PIL import Image as _PILImage
    orig = _PILImage.open(io.BytesIO(img_bytes))
    orig_w, orig_h = orig.size

    log.info("image op: %s  %dx%d  params=%s", operation, orig_w, orig_h, params)

    try:
        result_bytes, mime, new_w, new_h = await asyncio.wait_for(
            asyncio.to_thread(_process_image, img_bytes, operation, params),
            timeout=30.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Image processing timed out")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("image processing failed")
        raise HTTPException(status_code=500, detail=f"Processing error: {e}")

    b64_out = base64.b64encode(result_bytes).decode("ascii")

    log.info("image op done: %s  %dx%d → %dx%d  (%d KB)",
             operation, orig_w, orig_h, new_w, new_h, len(result_bytes) // 1024)

    return {
        "image": b64_out,
        "mime": mime,
        "width": new_w,
        "height": new_h,
        "original_width": orig_w,
        "original_height": orig_h,
        "operation": operation,
        "size": len(result_bytes),
    }


# ---------------------------------------------------------------------------
# State Synchronization & Auth
# ---------------------------------------------------------------------------

@app.post("/api/login")
async def login(req: Request) -> dict[str, str]:
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    pwd = body.get("password")
    if pwd == ADMIN_PASSWORD:
        token = secrets.token_hex(32)
        ADMIN_TOKENS.add(token)
        log.info("Admin login successful. Token generated.")
        return {"token": token}
    
    log.warning("Failed admin login attempt.")
    raise HTTPException(status_code=401, detail="Invalid password")

def _check_auth(req: Request):
    auth = req.headers.get("Authorization")
    if not auth or not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid token")
    token = auth.split(" ")[1]
    if token not in ADMIN_TOKENS:
        raise HTTPException(status_code=401, detail="Invalid token")

@app.get("/api/state")
async def get_state(req: Request) -> dict[str, Any]:
    """Retrieve the application state from the backend (admin only)."""
    _check_auth(req)
    if not STATE_FILE.exists():
        return {}
    
    try:
        def _read():
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return await asyncio.to_thread(_read)
    except Exception as e:
        log.error("Failed to read state.json: %s", e)
        raise HTTPException(status_code=500, detail="Failed to read state")

@app.post("/api/state")
async def save_state(req: Request) -> dict[str, bool]:
    """Save the application state to the backend (admin only)."""
    _check_auth(req)
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    try:
        def _write(data):
            # Atomic write to prevent corruption
            fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix="state_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp_path, STATE_FILE)
            
        await asyncio.to_thread(_write, body)
        return {"ok": True}
    except Exception as e:
        log.error("Failed to write state.json: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save state")


@app.post("/api/cancel/{model}")
async def cancel(model: str) -> dict:
    """Best-effort: ask Ollama to stop loading the given model.

    Ollama doesn't expose a true cancel endpoint, but closing the upstream
    connection (which happens when the browser aborts the fetch) is enough
    to stop generation. This is just a no-op endpoint kept for symmetry.
    """
    return {"ok": True, "model": model}


# Static assets (the index.html plus any future css/js files we split out)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def main() -> None:
    p = argparse.ArgumentParser(description="ShivaGPT server")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", default=8000, type=int)
    p.add_argument("--reload", action="store_true")
    p.add_argument("--debug", action="store_true",
                   help="Verbose logging (same as SHIVAGPT_DEBUG=1)")
    p.add_argument("--quiet", action="store_true",
                   help="Force INFO level even if SHIVAGPT_DEBUG is set in env")
    args = p.parse_args()

    # CLI flags override the env var so service operators can flip behavior
    # without restarting systemd.
    global DEBUG
    if args.debug and not args.quiet:
        DEBUG = True
        log.setLevel(logging.DEBUG)
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)
        log.info("Verbose debugging ENABLED via --debug flag")
    elif args.quiet:
        DEBUG = False
        log.setLevel(logging.INFO)
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.WARNING)

    import uvicorn

    log.info("ShivaGPT starting on http://%s:%d (proxying %s)  DEBUG=%s",
             args.host, args.port, OLLAMA_URL, DEBUG)
    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload,
                log_level="debug" if DEBUG else "info")


if __name__ == "__main__":
    main()
