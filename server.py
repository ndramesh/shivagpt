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
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "ndr123")
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
        upscayl_bin = DATA_DIR / "upscayl" / "resources" / "bin" / "upscayl-bin"
        models_dir = DATA_DIR / "upscayl" / "resources" / "models"

        if upscayl_bin.exists() and models_dir.exists():
            # remacri supports 2x, 3x, 4x per pass. The caller passes "factor".
            requested = int(params.get("factor", 4))
            scale = max(2, min(requested, 4))
            log.info("Using Upscayl for AI upscaling (%dx)...", scale)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f_in, \
                 tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f_out:
                img.save(f_in.name, format="PNG")
                cmd = [
                    str(upscayl_bin),
                    "-i", f_in.name,
                    "-o", f_out.name,
                    "-s", str(scale),
                    "-m", str(models_dir),
                    "-n", "remacri",
                ]
                try:
                    subprocess.run(cmd, check=True, capture_output=True)
                    img = Image.open(f_out.name)
                    img.load()
                except subprocess.CalledProcessError as e:
                    log.error("Upscayl failed: %s", e.stderr.decode() if e.stderr else e)
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
# Image Generation (/api/imgen)
#
# Supports multiple backends so you can pick speed vs quality vs license:
#   flux-schnell  — Apache 2.0, 4 steps, very fast, default
#   flux-dev      — best quality, gated on HF (needs HF_TOKEN), 20+ steps
#   sdxl          — legacy fallback
#
# Native resolution is capped at ~2048×2048 (beyond that, models degrade).
# To reach 4K / 8K / 16K, the endpoint chains the existing Upscayl pipeline
# (2× or 4× per pass, remacri model) on top of the diffusion output.
# ---------------------------------------------------------------------------

# Model registry. "load" is called once per model to construct a diffusers
# pipeline; the result is cached in _imgen_state.
IMGEN_MODELS: dict[str, dict[str, Any]] = {
    "flux-schnell": {
        "hf": "black-forest-labs/FLUX.1-schnell",
        "default_steps": 4,
        "default_guidance": 0.0,   # schnell is distilled, doesn't use CFG
        "use_cfg": False,
        "max_native": 2048,
        "pipeline_class": "FluxPipeline",
        "dtype": "bfloat16",
        "load_kwargs": {},
    },
    "flux-dev": {
        "hf": "black-forest-labs/FLUX.1-dev",
        "default_steps": 20,
        "default_guidance": 3.5,
        "use_cfg": True,
        "max_native": 2048,
        "pipeline_class": "FluxPipeline",
        "dtype": "bfloat16",
        "load_kwargs": {},
    },
    "sdxl": {
        "hf": "stabilityai/stable-diffusion-xl-base-1.0",
        "default_steps": 25,
        "default_guidance": 7.5,
        "use_cfg": True,
        "max_native": 1536,
        "pipeline_class": "AutoPipelineForText2Image",
        "dtype": "float16",
        "load_kwargs": {"variant": "fp16", "use_safetensors": True},
    },
    # Community fine-tunes of SDXL. These are dramatically better at
    # photorealistic faces, anatomy, and hands than vanilla SDXL Base. Both
    # are ungated on Hugging Face. ~6.5 GB each on first download.
    "realvis-xl": {
        "hf": "SG161222/RealVisXL_V5.0",
        "default_steps": 30,
        "default_guidance": 6.0,
        "use_cfg": True,
        "max_native": 1536,
        "pipeline_class": "AutoPipelineForText2Image",
        "dtype": "float16",
        "load_kwargs": {"use_safetensors": True},
    },
    "juggernaut-xl": {
        "hf": "RunDiffusion/Juggernaut-XL-v9",
        "default_steps": 30,
        "default_guidance": 6.5,
        "use_cfg": True,
        "max_native": 1536,
        "pipeline_class": "AutoPipelineForText2Image",
        "dtype": "float16",
        "load_kwargs": {"use_safetensors": True},
    },
}

# Friendly aliases so the user doesn't have to remember exact tags.
IMGEN_ALIASES: dict[str, str] = {
    "flux": "flux-schnell",
    "schnell": "flux-schnell",
    "dev": "flux-dev",
    "realvis": "realvis-xl",
    "realistic": "realvis-xl",
    "real": "realvis-xl",
    "juggernaut": "juggernaut-xl",
    "jug": "juggernaut-xl",
}

IMGEN_DEFAULT_MODEL = os.getenv("IMGEN_DEFAULT_MODEL", "flux-schnell")
IMGEN_MAX_OUTPUT_SIDE = int(os.getenv("IMGEN_MAX_OUTPUT_SIDE", "16384"))  # final-image side cap
IMGEN_TIMEOUT_S = float(os.getenv("IMGEN_TIMEOUT_S", "600"))

# Singleton pipeline cache. Only one model is kept resident at a time
# (each is ~10-25 GB VRAM; the DGX has plenty but no need to thrash).
_imgen_state: dict[str, Any] = {"model_key": None, "pipeline": None}


def _get_imgen_pipeline(model_key: str):
    """Lazy-load and cache the requested diffusers pipeline. Swaps if needed."""
    if _imgen_state["model_key"] == model_key and _imgen_state["pipeline"] is not None:
        return _imgen_state["pipeline"]

    import torch
    # Free the previous pipeline first so we don't double up on VRAM.
    if _imgen_state["pipeline"] is not None:
        log.info("imgen: unloading %s to make room for %s",
                 _imgen_state["model_key"], model_key)
        _imgen_state["pipeline"] = None
        torch.cuda.empty_cache()

    cfg = IMGEN_MODELS[model_key]
    log.info("imgen: loading %s (%s) ...", model_key, cfg["hf"])
    dtype = getattr(torch, cfg["dtype"])
    if cfg["pipeline_class"] == "FluxPipeline":
        from diffusers import FluxPipeline
        pipe = FluxPipeline.from_pretrained(cfg["hf"], torch_dtype=dtype, **cfg["load_kwargs"])
    else:
        from diffusers import AutoPipelineForText2Image
        pipe = AutoPipelineForText2Image.from_pretrained(cfg["hf"], torch_dtype=dtype, **cfg["load_kwargs"])
    pipe = pipe.to("cuda")
    _imgen_state["model_key"] = model_key
    _imgen_state["pipeline"] = pipe
    log.info("imgen: %s loaded", model_key)
    return pipe


def _parse_imgen_size(body: dict, cfg: dict) -> tuple[int, int]:
    """Resolve requested {width, height, size, aspect} to a concrete WxH.

    `size` alone -> square. `aspect` (e.g. "16:9") + `size` -> wide.
    """
    max_native = cfg["max_native"]
    width = body.get("width")
    height = body.get("height")
    size = body.get("size")
    aspect = (body.get("aspect") or "").strip()

    if not width and not height and size:
        # Accept "W:H" or "WxH" (and tolerate spaces) for the aspect ratio.
        sep = ":" if ":" in aspect else ("x" if "x" in aspect.lower() else None)
        if sep:
            try:
                ar_w, ar_h = (float(p.strip()) for p in aspect.lower().split(sep, 1))
            except ValueError:
                ar_w, ar_h = 1.0, 1.0
            if ar_w >= ar_h:
                width = int(size)
                height = int(size * ar_h / ar_w)
            else:
                height = int(size)
                width = int(size * ar_w / ar_h)
        else:
            width = height = int(size)

    width = int(width or 1024)
    height = int(height or 1024)
    width = max(256, min(width, max_native))
    height = max(256, min(height, max_native))

    # FLUX needs side lengths that are multiples of 16; SDXL needs 8.
    align = 16 if cfg["pipeline_class"] == "FluxPipeline" else 8
    width = (width // align) * align
    height = (height // align) * align
    return width, height


def _upscale_chain(img_bytes: bytes, factor: int) -> tuple[bytes, str, int, int]:
    """Pass image through Upscayl 2x/4x at a time until we hit the target factor.

    Returns (bytes, mime, width, height). Falls back to PIL Lanczos when
    Upscayl isn't available. If a pass fails we keep whatever we've got.
    """
    if factor <= 1:
        from PIL import Image as _PIL
        im = _PIL.open(io.BytesIO(img_bytes))
        return img_bytes, "image/png", im.width, im.height

    remaining = factor
    cur_bytes = img_bytes
    cur_mime = "image/png"
    last_w = last_h = 0
    while remaining >= 2:
        step = 4 if remaining >= 4 else 2
        log.info("imgen: upscale pass %dx (remaining factor %d)", step, remaining)
        try:
            cur_bytes, cur_mime, last_w, last_h = _process_image(
                cur_bytes, "upscale", {"factor": step}
            )
        except Exception as e:
            log.warning("imgen: upscale stopped at %dx (remaining %d): %s",
                        factor // remaining, remaining, e)
            break
        remaining //= step

    return cur_bytes, cur_mime, last_w, last_h


@app.post("/api/imgen")
async def process_imgen(req: Request) -> dict[str, Any]:
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Missing 'prompt'")

    model_key = (body.get("model") or IMGEN_DEFAULT_MODEL).lower()
    model_key = IMGEN_ALIASES.get(model_key, model_key)
    if model_key not in IMGEN_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown imgen model {model_key!r}. Pick from: {sorted(IMGEN_MODELS)}",
        )
    cfg = IMGEN_MODELS[model_key]

    width, height = _parse_imgen_size(body, cfg)

    steps = int(body.get("steps") or cfg["default_steps"])
    steps = max(1, min(steps, 100))
    try:
        guidance = float(body.get("guidance", cfg["default_guidance"]))
    except (TypeError, ValueError):
        guidance = cfg["default_guidance"]
    upscale = int(body.get("upscale") or 1)
    upscale = max(1, min(upscale, 16))   # capped at 16x (e.g. 1024 -> 16384)
    seed = body.get("seed")
    negative_prompt = (body.get("negative_prompt")
                       or "ugly, deformed, extra limbs, extra fingers, bad anatomy, "
                          "blurry, worst quality, low resolution, jpeg artifacts")

    # Final-size sanity check
    final_w = width * upscale
    final_h = height * upscale
    if max(final_w, final_h) > IMGEN_MAX_OUTPUT_SIDE:
        raise HTTPException(
            status_code=400,
            detail=f"Final size {final_w}x{final_h} exceeds cap "
                   f"({IMGEN_MAX_OUTPUT_SIDE}px per side). Lower size or upscale factor.",
        )

    log.info("imgen: model=%s native=%dx%d steps=%d guidance=%.1f upscale=%dx "
             "final=%dx%d prompt=%r",
             model_key, width, height, steps, guidance, upscale,
             final_w, final_h, prompt[:80])

    def _generate() -> bytes:
        import torch
        pipe = _get_imgen_pipeline(model_key)
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "num_inference_steps": steps,
            "width": width,
            "height": height,
        }
        if cfg["use_cfg"]:
            kwargs["guidance_scale"] = guidance
            # FluxPipeline accepts negative_prompt only on FLUX.1-dev; schnell ignores it.
            if cfg["pipeline_class"] != "FluxPipeline":
                kwargs["negative_prompt"] = negative_prompt
        if seed is not None:
            try:
                kwargs["generator"] = torch.Generator(device="cuda").manual_seed(int(seed))
            except (TypeError, ValueError):
                pass
        image = pipe(**kwargs).images[0]
        out = io.BytesIO()
        image.save(out, format="PNG")
        return out.getvalue()

    t_gen0 = time.monotonic()
    try:
        native_bytes = await asyncio.wait_for(
            asyncio.to_thread(_generate),
            timeout=IMGEN_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504,
                            detail=f"Image generation timed out after {IMGEN_TIMEOUT_S:.0f}s")
    except Exception as e:
        log.exception("imgen failed")
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")
    t_gen = time.monotonic() - t_gen0

    # Optional AI-upscale chain
    t_up0 = time.monotonic()
    if upscale > 1:
        out_bytes, out_mime, out_w, out_h = await asyncio.wait_for(
            asyncio.to_thread(_upscale_chain, native_bytes, upscale),
            timeout=IMGEN_TIMEOUT_S,
        )
    else:
        out_bytes = native_bytes
        out_mime = "image/png"
        out_w, out_h = width, height
    t_up = time.monotonic() - t_up0

    # JPEG-encode huge outputs to keep the response payload manageable.
    if max(out_w, out_h) >= 4096 and out_mime != "image/jpeg":
        from PIL import Image as _PIL
        img = _PIL.open(io.BytesIO(out_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92, optimize=True)
        out_bytes = buf.getvalue()
        out_mime = "image/jpeg"

    log.info("imgen: done %dx%d %s (%d KB)  gen=%.1fs upscale=%.1fs model=%s",
             out_w, out_h, out_mime, len(out_bytes) // 1024, t_gen, t_up, model_key)

    return {
        "image": base64.b64encode(out_bytes).decode("ascii"),
        "mime": out_mime,
        "width": out_w,
        "height": out_h,
        "model": model_key,
        "size": len(out_bytes),
        "native_width": width,
        "native_height": height,
        "upscale": upscale,
        "gen_seconds": round(t_gen, 2),
        "upscale_seconds": round(t_up, 2),
    }


@app.get("/api/imgen/models")
async def imgen_models() -> dict[str, Any]:
    """List the imgen models the server knows about (for the UI dropdown)."""
    return {
        "default": IMGEN_DEFAULT_MODEL,
        "models": [
            {"key": k, "hf": v["hf"], "max_native": v["max_native"],
             "default_steps": v["default_steps"], "uses_cfg": v["use_cfg"]}
            for k, v in IMGEN_MODELS.items()
        ],
        "max_output_side": IMGEN_MAX_OUTPUT_SIDE,
    }


# ---------------------------------------------------------------------------
# Stock market data (/api/stock/*)
#
# NOT financial advice — these endpoints surface publicly available market
# data so the user can make their own decisions. Two data sources:
#   - Alpaca (primary): real-time quotes, bars, options. Uses
#     APCA_API_KEY_ID / APCA_API_SECRET_KEY env vars. Free-tier IEX feed.
#   - yfinance (fallback / supplemental): fundamentals (P/E, EPS, market
#     cap, beta, sector, summary), Wall Street analyst consensus, news
#     headlines — none of which Alpaca exposes.
#
# Each endpoint runs both sources in parallel where useful and merges,
# preferring Alpaca for prices and yfinance for company info. Anything
# labeled "consensus" or "rating" is third-party analyst data, not
# generated by this server.
# ---------------------------------------------------------------------------

STOCK_DISCLAIMER = (
    "_Prices via Alpaca (IEX feed, real-time-ish); fundamentals / news / "
    "analyst consensus via Yahoo Finance. Analyst ratings are third-party "
    "Wall Street consensus, not recommendations from this server. "
    "For research only — not financial advice._"
)


def _yfinance_or_none():
    """Lazy import so the server starts even if yfinance isn't installed yet."""
    try:
        import yfinance  # noqa: F401
        return yfinance
    except ImportError as e:
        log.warning("yfinance not installed: %s", e)
        return None


def _alpaca_keys() -> tuple[str, str] | None:
    """Read Alpaca credentials from env. Returns (key, secret) or None."""
    key = (os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_API_KEY_ID")
           or os.getenv("ALPACA_KEY") or "").strip()
    sec = (os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_API_SECRET_KEY")
           or os.getenv("ALPACA_SECRET") or "").strip()
    return (key, sec) if (key and sec) else None


def _alpaca_stock_client():
    """Lazy import + cache the Alpaca historical-data client."""
    keys = _alpaca_keys()
    if not keys:
        return None
    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient
    except ImportError as e:
        log.warning("alpaca-py not installed: %s", e)
        return None
    return StockHistoricalDataClient(keys[0], keys[1])


def _alpaca_options_client():
    keys = _alpaca_keys()
    if not keys:
        return None
    try:
        from alpaca.data.historical.option import OptionHistoricalDataClient
    except ImportError:
        return None
    return OptionHistoricalDataClient(keys[0], keys[1])


def _alpaca_trading_client():
    """For options-chain endpoint metadata (assets, options-contracts list)."""
    keys = _alpaca_keys()
    if not keys:
        return None
    try:
        from alpaca.trading.client import TradingClient
    except ImportError:
        return None
    base = os.getenv("APCA_API_BASE_URL", "")
    paper = "paper" in base.lower() if base else True   # default: paper
    return TradingClient(keys[0], keys[1], paper=paper)


def _alpaca_quote_and_bars(ticker: str, days: int = 30) -> dict[str, Any] | None:
    """Returns {price, prev_close, change, ..., sparkline, history_df} from Alpaca."""
    cli = _alpaca_stock_client()
    if cli is None:
        return None
    try:
        from datetime import datetime, timedelta, timezone
        from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest
        from alpaca.data.timeframe import TimeFrame

        # Latest quote (bid/ask) and latest trade (price)
        try:
            qresp = cli.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=ticker))
            q = qresp.get(ticker) if isinstance(qresp, dict) else None
        except Exception:
            q = None
        try:
            tresp = cli.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=ticker))
            tr = tresp.get(ticker) if isinstance(tresp, dict) else None
        except Exception:
            tr = None

        # Daily bars for the last `days` calendar days
        # Use a slightly bigger window to be safe against weekends/holidays
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(days * 1.6) + 5)
        bars_resp = cli.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=start, end=end,
        ))
        bars = bars_resp.data.get(ticker, []) if hasattr(bars_resp, "data") else \
               (bars_resp.get(ticker, []) if isinstance(bars_resp, dict) else [])
        if not bars:
            return None

        last_bar = bars[-1]
        prev_bar = bars[-2] if len(bars) >= 2 else last_bar
        price = float(tr.price) if tr and getattr(tr, "price", None) else float(last_bar.close)
        prev_close = float(prev_bar.close)

        sparkline = [{"t": b.timestamp.strftime("%Y-%m-%d") if hasattr(b.timestamp, "strftime")
                      else str(b.timestamp),
                      "c": float(b.close)} for b in bars[-days:]]

        return {
            "price": price,
            "previous_close": prev_close,
            "change": price - prev_close,
            "change_pct": ((price - prev_close) / prev_close * 100) if prev_close else 0.0,
            "open": float(last_bar.open),
            "day_high": float(last_bar.high),
            "day_low": float(last_bar.low),
            "volume": int(last_bar.volume),
            "as_of": str(last_bar.timestamp),
            "bid": float(q.bid_price) if q and getattr(q, "bid_price", None) else None,
            "ask": float(q.ask_price) if q and getattr(q, "ask_price", None) else None,
            "sparkline": sparkline,
            "source": "alpaca",
        }
    except Exception as e:
        log.warning("alpaca quote/bars failed for %s: %s", ticker, e)
        return None


def _alpaca_bars_for_analysis(ticker: str, lookback_days: int = 365):
    """Returns a pandas Series of closing prices indexed by date, or None."""
    cli = _alpaca_stock_client()
    if cli is None:
        return None
    try:
        import pandas as pd
        from datetime import datetime, timedelta, timezone
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days + 30)
        resp = cli.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=start, end=end,
        ))
        bars = resp.data.get(ticker, []) if hasattr(resp, "data") else \
               (resp.get(ticker, []) if isinstance(resp, dict) else [])
        if not bars:
            return None
        s = pd.Series([float(b.close) for b in bars],
                      index=pd.to_datetime([b.timestamp for b in bars]),
                      name="close")
        return s
    except Exception as e:
        log.warning("alpaca bars (analysis) failed for %s: %s", ticker, e)
        return None


def _fmt_num(v: Any) -> str:
    """Compact number formatter for market caps etc."""
    if v is None:
        return "n/a"
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    a = abs(n)
    if a >= 1e12:
        return f"{n / 1e12:.2f}T"
    if a >= 1e9:
        return f"{n / 1e9:.2f}B"
    if a >= 1e6:
        return f"{n / 1e6:.2f}M"
    if a >= 1e3:
        return f"{n / 1e3:.2f}K"
    return f"{n:.2f}"


def _compute_technicals(close_series) -> dict[str, Any]:
    """Hand-rolled RSI/MACD/SMA/Bollinger so we don't pull in pandas-ta.

    Expects a pandas Series of closing prices, oldest first."""
    n = len(close_series)
    if n < 20:
        return {"error": f"Need at least 20 price points; got {n}"}

    sma_20 = close_series.rolling(20).mean().iloc[-1]
    sma_50 = close_series.rolling(50).mean().iloc[-1] if n >= 50 else None
    sma_200 = close_series.rolling(200).mean().iloc[-1] if n >= 200 else None

    # RSI (14) — Wilder's smoothing approximated by simple rolling mean is fine
    delta = close_series.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = (100 - 100 / (1 + rs)).iloc[-1]

    # MACD (12, 26, 9)
    ema12 = close_series.ewm(span=12, adjust=False).mean()
    ema26 = close_series.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()

    # Bollinger Bands (20, 2σ)
    bb_mid = close_series.rolling(20).mean()
    bb_std = close_series.rolling(20).std()
    bb_upper = bb_mid + 2 * bb_std
    bb_lower = bb_mid - 2 * bb_std

    def _f(x):
        try:
            x = float(x)
            return x if x == x else None  # NaN check
        except (TypeError, ValueError):
            return None

    return {
        "sma_20": _f(sma_20),
        "sma_50": _f(sma_50),
        "sma_200": _f(sma_200),
        "rsi_14": _f(rsi),
        "macd": _f(macd_line.iloc[-1]),
        "macd_signal": _f(signal_line.iloc[-1]),
        "macd_hist": _f(macd_line.iloc[-1] - signal_line.iloc[-1]),
        "bb_upper": _f(bb_upper.iloc[-1]),
        "bb_middle": _f(bb_mid.iloc[-1]),
        "bb_lower": _f(bb_lower.iloc[-1]),
    }


def _interpret_technicals(price: float, ind: dict) -> list[dict]:
    """Translate indicator values into textbook readings WITHOUT issuing
    buy/sell calls. Each entry includes the reading and a short note about
    what traders traditionally take from it."""
    out = []
    rsi = ind.get("rsi_14")
    if rsi is not None:
        if rsi >= 70:
            out.append({"name": "RSI(14)", "value": round(rsi, 1), "reading": "overbought",
                        "note": "Traditionally read as overextended; can stay overbought for a while in strong trends."})
        elif rsi <= 30:
            out.append({"name": "RSI(14)", "value": round(rsi, 1), "reading": "oversold",
                        "note": "Traditionally read as oversold; oversold conditions can persist."})
        else:
            out.append({"name": "RSI(14)", "value": round(rsi, 1), "reading": "neutral",
                        "note": "Within the 30–70 neutral band."})

    sma50 = ind.get("sma_50")
    sma200 = ind.get("sma_200")
    if sma50 is not None and sma200 is not None:
        if price > sma50 > sma200:
            out.append({"name": "Moving averages", "reading": "uptrend",
                        "note": f"Price > 50-day (${sma50:.2f}) > 200-day (${sma200:.2f}) — classic uptrend alignment."})
        elif price < sma50 < sma200:
            out.append({"name": "Moving averages", "reading": "downtrend",
                        "note": f"Price < 50-day (${sma50:.2f}) < 200-day (${sma200:.2f}) — classic downtrend alignment."})
        elif sma50 > sma200 and price < sma50:
            out.append({"name": "Moving averages", "reading": "uptrend, near-term pullback",
                        "note": f"Long-term uptrend (50-day > 200-day) but price has dipped below the 50-day."})
        else:
            out.append({"name": "Moving averages", "reading": "mixed",
                        "note": f"50-day ${sma50:.2f}, 200-day ${sma200:.2f} — no clear alignment with price."})

    macd = ind.get("macd")
    sig = ind.get("macd_signal")
    if macd is not None and sig is not None:
        if macd > sig:
            out.append({"name": "MACD(12,26,9)", "reading": "above signal line",
                        "note": "MACD above signal — traders traditionally read this as bullish momentum, especially when the histogram is widening."})
        else:
            out.append({"name": "MACD(12,26,9)", "reading": "below signal line",
                        "note": "MACD below signal — traders traditionally read this as bearish momentum."})

    bb_u = ind.get("bb_upper")
    bb_l = ind.get("bb_lower")
    if bb_u is not None and bb_l is not None:
        if price > bb_u:
            out.append({"name": "Bollinger(20,2)", "reading": "above upper band",
                        "note": f"Above upper band (${bb_u:.2f}) — historically rare; mean-reversion traders might watch for a pullback."})
        elif price < bb_l:
            out.append({"name": "Bollinger(20,2)", "reading": "below lower band",
                        "note": f"Below lower band (${bb_l:.2f}) — historically rare; mean-reversion traders might watch for a bounce."})
        else:
            out.append({"name": "Bollinger(20,2)", "reading": "inside bands",
                        "note": f"Within ${bb_l:.2f}–${bb_u:.2f}."})

    return out


def _yf_company_info(ticker: str) -> dict[str, Any]:
    """Fundamentals + summary from yfinance. Best-effort; never raises."""
    yf = _yfinance_or_none()
    if yf is None:
        return {}
    try:
        t = yf.Ticker(ticker)
        info = {}
        try:
            info = t.info or {}
        except Exception:
            pass
        fast = {}
        try:
            fast = dict(t.fast_info) if hasattr(t, "fast_info") else {}
        except Exception:
            pass
        return {
            "name": info.get("longName") or info.get("shortName"),
            "exchange": info.get("exchange") or fast.get("exchange"),
            "currency": info.get("currency") or fast.get("currency") or "USD",
            "avg_volume": info.get("averageVolume"),
            "fifty_two_week_high": info.get("fiftyTwoWeekHigh") or fast.get("year_high"),
            "fifty_two_week_low": info.get("fiftyTwoWeekLow") or fast.get("year_low"),
            "market_cap": info.get("marketCap") or fast.get("market_cap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "eps": info.get("trailingEps"),
            "dividend_yield": info.get("dividendYield"),
            "beta": info.get("beta"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "summary": (info.get("longBusinessSummary") or "")[:1500],
        }
    except Exception as e:
        log.debug("yf_company_info failed for %s: %s", ticker, e)
        return {}


@app.post("/api/stock/quote")
async def stock_quote(req: Request) -> dict[str, Any]:
    """Real-time quote (Alpaca) + fundamentals (yfinance) + recent sparkline."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Missing 'ticker'")

    # Fire Alpaca + yfinance in parallel; both are I/O bound.
    alp_task = asyncio.to_thread(_alpaca_quote_and_bars, ticker, 30)
    yf_task = asyncio.to_thread(_yf_company_info, ticker)
    try:
        alp, info = await asyncio.wait_for(
            asyncio.gather(alp_task, yf_task), timeout=20
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Quote fetch timed out")

    if alp is None:
        # No Alpaca data — fall back to a yfinance-only quote so /stock still works.
        yf = _yfinance_or_none()
        if yf is None:
            raise HTTPException(
                status_code=503,
                detail="No price source available. Configure APCA_API_KEY_ID/APCA_API_SECRET_KEY "
                       "or install yfinance on the server.",
            )
        def _yf_fallback():
            t = yf.Ticker(ticker)
            hist = t.history(period="1mo", auto_adjust=False)
            if hist.empty:
                return None
            last = hist.iloc[-1]
            prev = hist.iloc[-2] if len(hist) >= 2 else last
            price = float(last.Close); prev_close = float(prev.Close)
            return {
                "price": price, "previous_close": prev_close,
                "change": price - prev_close,
                "change_pct": ((price - prev_close) / prev_close * 100) if prev_close else 0.0,
                "open": float(last.Open), "day_high": float(last.High), "day_low": float(last.Low),
                "volume": int(last.Volume), "as_of": str(hist.index[-1]),
                "sparkline": [{"t": i.strftime("%Y-%m-%d"), "c": float(r.Close)}
                              for i, r in hist.iterrows()],
                "source": "yfinance (delayed ~15min)",
            }
        alp = await asyncio.to_thread(_yf_fallback)
        if alp is None:
            raise HTTPException(status_code=404,
                                detail=f"No data for ticker {ticker!r}")

    merged = {
        "ticker": ticker,
        **info,
        **alp,
        "name": info.get("name") or ticker,
        "disclaimer": STOCK_DISCLAIMER,
        "data_sources": {
            "prices": alp.get("source", "alpaca"),
            "fundamentals": "yfinance" if info else "unavailable",
        },
    }
    return merged


def _yf_consensus_and_news(ticker: str) -> dict[str, Any]:
    """Analyst consensus + news from yfinance. Best-effort; never raises."""
    yf = _yfinance_or_none()
    if yf is None:
        return {"consensus": {}, "news": []}
    try:
        t = yf.Ticker(ticker)
        consensus: dict[str, Any] = {}
        try:
            info = t.info or {}
            consensus["mean_rating"] = info.get("recommendationMean")
            consensus["recommendation_key"] = info.get("recommendationKey")
            consensus["target_mean_price"] = info.get("targetMeanPrice")
            consensus["target_high_price"] = info.get("targetHighPrice")
            consensus["target_low_price"] = info.get("targetLowPrice")
            consensus["number_of_analysts"] = info.get("numberOfAnalystOpinions")
        except Exception:
            pass
        try:
            rec_df = t.recommendations_summary
            if rec_df is not None and not rec_df.empty:
                latest = rec_df.iloc[0].to_dict()
                consensus["latest_summary"] = {
                    k: int(v) if isinstance(v, (int, float)) and v == v else v
                    for k, v in latest.items()
                }
        except Exception:
            pass

        news = []
        try:
            for item in (t.news or [])[:8]:
                content = item.get("content") or item
                news.append({
                    "title": content.get("title") or item.get("title"),
                    "publisher": (content.get("provider") or {}).get("displayName")
                                  or item.get("publisher"),
                    "link": (content.get("canonicalUrl") or {}).get("url")
                             or content.get("clickThroughUrl", {}).get("url")
                             or item.get("link"),
                    "published": content.get("pubDate") or item.get("providerPublishTime"),
                })
        except Exception:
            pass
        return {"consensus": consensus, "news": news}
    except Exception as e:
        log.debug("yf_consensus_and_news failed for %s: %s", ticker, e)
        return {"consensus": {}, "news": []}


@app.post("/api/stock/analysis")
async def stock_analysis(req: Request) -> dict[str, Any]:
    """Technicals from Alpaca daily bars + analyst consensus + news from yfinance."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Missing 'ticker'")
    period_days = int(body.get("period_days") or 365)

    # Try Alpaca for bars; fall back to yfinance.
    alp_close = await asyncio.to_thread(_alpaca_bars_for_analysis, ticker, period_days)
    bars_source = "alpaca"
    if alp_close is None:
        # yfinance fallback
        yf = _yfinance_or_none()
        if yf is None:
            raise HTTPException(
                status_code=503,
                detail="No bar source available. Configure Alpaca keys or install yfinance.",
            )
        def _yf_bars():
            t = yf.Ticker(ticker)
            hist = t.history(period="2y" if period_days > 365 else "1y", auto_adjust=True)
            return hist["Close"] if not hist.empty else None
        alp_close = await asyncio.to_thread(_yf_bars)
        bars_source = "yfinance (delayed)"
        if alp_close is None or len(alp_close) < 20:
            raise HTTPException(status_code=404, detail=f"No bar data for ticker {ticker!r}")

    price = float(alp_close.iloc[-1])
    indicators = _compute_technicals(alp_close)
    readings = _interpret_technicals(price, indicators)

    # Run yfinance consensus/news in parallel.
    extras = await asyncio.to_thread(_yf_consensus_and_news, ticker)

    return {
        "ticker": ticker,
        "current_price": price,
        "indicators": indicators,
        "readings": readings,
        "analyst_consensus": extras["consensus"],
        "news": extras["news"],
        "history_points": int(len(alp_close)),
        "period_days": period_days,
        "disclaimer": STOCK_DISCLAIMER,
        "data_sources": {
            "bars": bars_source,
            "consensus_news": "yfinance" if extras["consensus"] or extras["news"] else "unavailable",
        },
    }


def _options_strategy_math(price: float, calls: list, puts: list) -> list[dict]:
    """Compute payoff math for a handful of common strategies near ATM.

    Each strategy entry includes: name, description, breakeven(s),
    max_profit, max_loss, and the strikes/premiums it assumes. We do NOT
    rank them or recommend one — the user picks based on their thesis.
    """
    out: list[dict] = []

    def _atm(rows, side):
        rows = [r for r in rows if r.get("bid", 0) and r.get("ask", 0)]
        if not rows:
            return None
        rows.sort(key=lambda r: abs(r["strike"] - price))
        return rows[0]

    atm_call = _atm(calls, "call")
    atm_put = _atm(puts, "put")

    # 1. Covered call: own 100 shares, sell 1 ATM call.
    if atm_call:
        prem = (atm_call["bid"] + atm_call["ask"]) / 2.0
        k = atm_call["strike"]
        out.append({
            "name": "Covered Call (ATM)",
            "description": "Own 100 shares, sell 1 ATM call. Income up front, but capped upside.",
            "strikes": {"short_call": k},
            "premium_collected": round(prem * 100, 2),
            "breakeven": round(price - prem, 2),
            "max_profit": round((k - price + prem) * 100, 2),
            "max_loss": "≈ shares' cost basis × 100 minus premium (full downside on the stock)",
            "best_if": "stock drifts sideways or up slowly through the strike by expiry",
        })

    # 2. Cash-secured put: hold cash, sell 1 ATM put.
    if atm_put:
        prem = (atm_put["bid"] + atm_put["ask"]) / 2.0
        k = atm_put["strike"]
        out.append({
            "name": "Cash-Secured Put (ATM)",
            "description": "Hold cash to cover, sell 1 ATM put. Income now; assigned shares at strike if price falls below.",
            "strikes": {"short_put": k},
            "premium_collected": round(prem * 100, 2),
            "breakeven": round(k - prem, 2),
            "max_profit": round(prem * 100, 2),
            "max_loss": round((k - prem) * 100, 2),
            "best_if": "stock stays above the strike OR you want to own shares at a discount",
        })

    # 3. Vertical bull call spread (ATM long, +1 strike short)
    sorted_calls = sorted(calls, key=lambda r: r["strike"])
    if len(sorted_calls) >= 2:
        atm_idx = min(range(len(sorted_calls)),
                      key=lambda i: abs(sorted_calls[i]["strike"] - price))
        if atm_idx + 1 < len(sorted_calls):
            long_c = sorted_calls[atm_idx]
            short_c = sorted_calls[atm_idx + 1]
            long_prem = (long_c["bid"] + long_c["ask"]) / 2.0
            short_prem = (short_c["bid"] + short_c["ask"]) / 2.0
            debit = long_prem - short_prem
            spread = short_c["strike"] - long_c["strike"]
            if debit > 0 and spread > 0:
                out.append({
                    "name": "Bull Call Spread (ATM + 1 strike)",
                    "description": f"Buy {long_c['strike']:.0f} call, sell {short_c['strike']:.0f} call. Limited risk, limited reward, bullish.",
                    "strikes": {"long_call": long_c["strike"], "short_call": short_c["strike"]},
                    "net_debit": round(debit * 100, 2),
                    "breakeven": round(long_c["strike"] + debit, 2),
                    "max_profit": round((spread - debit) * 100, 2),
                    "max_loss": round(debit * 100, 2),
                    "best_if": "you expect the stock to rise modestly by expiry",
                })

    return out


@app.post("/api/stock/options")
async def stock_options(req: Request) -> dict[str, Any]:
    """Options chain near at-the-money plus payoff math for common strategies."""
    yf = _yfinance_or_none()
    if yf is None:
        raise HTTPException(status_code=500, detail="yfinance is not installed on the server")
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    ticker = (body.get("ticker") or "").strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="Missing 'ticker'")
    requested_expiry = (body.get("expiry") or "").strip()
    n_strikes = max(4, min(int(body.get("strikes_each_side") or 6), 15))

    def _do() -> dict[str, Any] | None:
        t = yf.Ticker(ticker)
        expirations = list(t.options or [])
        if not expirations:
            return None
        # If user requested a specific expiry that exists, use it; else nearest.
        target = requested_expiry if requested_expiry in expirations else expirations[0]

        chain = t.option_chain(target)
        # Current underlying price for ATM detection
        hist = t.history(period="5d")
        price = float(hist["Close"].iloc[-1])

        def _select_near(df, n=n_strikes):
            df = df.assign(distance=(df["strike"] - price).abs()).sort_values("distance")
            keep = df.head(n * 2).sort_values("strike")
            keep = keep.drop(columns=["distance"])
            return keep

        cols = ["contractSymbol", "strike", "lastPrice", "bid", "ask",
                "change", "percentChange", "volume", "openInterest",
                "impliedVolatility", "inTheMoney"]
        def _records(df):
            df = _select_near(df)
            df = df[[c for c in cols if c in df.columns]]
            return df.to_dict("records")

        calls = _records(chain.calls)
        puts = _records(chain.puts)

        strategies = _options_strategy_math(price, calls, puts)

        return {
            "ticker": ticker,
            "current_price": price,
            "expiry": target,
            "available_expirations": expirations[:20],
            "calls": calls,
            "puts": puts,
            "strategies": strategies,
            "disclaimer": STOCK_DISCLAIMER,
        }

    try:
        result = await asyncio.wait_for(asyncio.to_thread(_do), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Options fetch timed out")
    except Exception as e:
        log.exception("stock/options failed for %s", ticker)
        raise HTTPException(status_code=502, detail=f"yfinance error: {e}")
    if not result:
        raise HTTPException(status_code=404, detail=f"No options data for ticker {ticker!r}")
    return result


# ---------------------------------------------------------------------------
# Portfolio (/api/portfolio) — read-only Alpaca account + positions
#
# READ-ONLY by construction: we only call get_account() and get_all_positions()
# on the Alpaca TradingClient. No order submission, no order cancellation, no
# position modifications. This server cannot place a trade.
# ---------------------------------------------------------------------------

@app.post("/api/portfolio")
async def portfolio(req: Request) -> dict[str, Any]:
    """Return Alpaca account balances + open positions. Requires APCA keys.

    Strictly read-only — uses TradingClient.get_account() and get_all_positions()
    only. No order endpoints touched.
    """
    tc = _alpaca_trading_client()
    if tc is None:
        raise HTTPException(
            status_code=503,
            detail="Alpaca trading client not available. Set APCA_API_KEY_ID and "
                   "APCA_API_SECRET_KEY in the systemd unit, and ensure alpaca-py is installed.",
        )

    def _do() -> dict[str, Any]:
        try:
            account = tc.get_account()
            positions = tc.get_all_positions()
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Alpaca error: {e}")

        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None

        equity = _f(account.equity)
        last_equity = _f(account.last_equity)
        day_pl = (equity - last_equity) if equity is not None and last_equity is not None else None
        day_pl_pct = (day_pl / last_equity * 100) if (day_pl is not None and last_equity) else None

        return {
            "account": {
                "status": str(account.status),
                "currency": account.currency,
                "cash": _f(account.cash),
                "equity": equity,
                "last_equity": last_equity,
                "buying_power": _f(account.buying_power),
                "portfolio_value": _f(account.portfolio_value),
                "day_pl": day_pl,
                "day_pl_pct": day_pl_pct,
                "daytrade_count": int(getattr(account, "daytrade_count", 0) or 0),
                "pattern_day_trader": bool(getattr(account, "pattern_day_trader", False)),
                "trading_blocked": bool(getattr(account, "trading_blocked", False)),
                "account_number": getattr(account, "account_number", None),
                "is_paper": "paper" in (os.getenv("APCA_API_BASE_URL", "") or "paper").lower(),
            },
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": _f(p.qty),
                    "avg_entry_price": _f(p.avg_entry_price),
                    "current_price": _f(getattr(p, "current_price", None)),
                    "market_value": _f(p.market_value),
                    "cost_basis": _f(p.cost_basis),
                    "unrealized_pl": _f(p.unrealized_pl),
                    "unrealized_plpc": _f(p.unrealized_plpc) * 100 if _f(p.unrealized_plpc) is not None else None,
                    "unrealized_intraday_pl": _f(p.unrealized_intraday_pl),
                    "unrealized_intraday_plpc": _f(p.unrealized_intraday_plpc) * 100 if _f(p.unrealized_intraday_plpc) is not None else None,
                    "side": str(p.side),
                    "asset_class": str(getattr(p, "asset_class", "us_equity")),
                }
                for p in positions
            ],
            "disclaimer": STOCK_DISCLAIMER,
        }

    try:
        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=15)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Portfolio fetch timed out")


# ---------------------------------------------------------------------------
# ThinkOrSwim / Schwab portfolio (/api/tp)
#
# Schwab acquired TD Ameritrade in 2020, so the ThinkOrSwim account is now a
# Schwab account, accessed through the Schwab Developer API (OAuth2). Setup
# requires a one-time browser-based auth flow — see scripts/schwab_auth.py.
#
# READ-ONLY by construction: we only call get_account_numbers() and
# get_account(..., fields=POSITIONS). No order endpoints touched anywhere.
# ---------------------------------------------------------------------------

SCHWAB_TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH", str(DATA_DIR / "schwab-token.json"))


def _schwab_client():
    """Return a ready-to-use Schwab client if credentials + token are present.

    Returns None (rather than raising) when not configured, so /api/tp can
    surface a helpful "needs setup" message instead of a 500.
    """
    key = (os.getenv("SCHWAB_APP_KEY") or os.getenv("SCHWAB_API_KEY") or "").strip()
    secret = (os.getenv("SCHWAB_APP_SECRET") or os.getenv("SCHWAB_API_SECRET") or "").strip()
    if not key or not secret:
        return None
    if not Path(SCHWAB_TOKEN_PATH).exists():
        log.warning("Schwab token file not found at %s — run scripts/schwab_auth.py once",
                    SCHWAB_TOKEN_PATH)
        return None
    try:
        from schwab.auth import client_from_token_file
    except ImportError as e:
        log.warning("schwab-py not installed: %s", e)
        return None
    try:
        return client_from_token_file(
            token_path=SCHWAB_TOKEN_PATH,
            api_key=key,
            app_secret=secret,
        )
    except Exception as e:
        log.warning("Schwab client init failed (refresh token may be expired): %s", e)
        return None


def _format_schwab_position(p: dict) -> dict[str, Any]:
    instrument = p.get("instrument", {}) or {}
    long_qty = float(p.get("longQuantity") or 0)
    short_qty = float(p.get("shortQuantity") or 0)
    qty = long_qty - short_qty
    return {
        "symbol": instrument.get("symbol"),
        "asset_type": instrument.get("assetType"),
        "description": instrument.get("description"),
        "qty": qty,
        "side": "long" if long_qty > 0 else ("short" if short_qty > 0 else "flat"),
        "avg_price": p.get("averagePrice"),
        "market_value": p.get("marketValue"),
        "current_day_pl": p.get("currentDayProfitLoss"),
        "current_day_pl_pct": p.get("currentDayProfitLossPercentage"),
        "long_open_pl": p.get("longOpenProfitLoss"),
        "short_open_pl": p.get("shortOpenProfitLoss"),
        "settled_long_qty": p.get("settledLongQuantity"),
        "settled_short_qty": p.get("settledShortQuantity"),
    }


@app.post("/api/tp")
async def tp_portfolio(req: Request) -> dict[str, Any]:
    """Return Schwab/ThinkOrSwim account balances + positions. READ-ONLY.

    Only call sites: schwab-py's get_account_numbers() and
    get_account(account_hash, fields=POSITIONS). No order endpoints.
    """
    c = _schwab_client()
    if c is None:
        raise HTTPException(
            status_code=503,
            detail="Schwab not configured. Steps: (1) register an app at "
                   "developer.schwab.com, (2) set SCHWAB_APP_KEY + "
                   "SCHWAB_APP_SECRET in the systemd unit, "
                   "(3) run scripts/schwab_auth.py once to populate "
                   f"the token file at {SCHWAB_TOKEN_PATH}.",
        )

    def _do() -> dict[str, Any]:
        from schwab.client import Client
        try:
            acc_numbers_resp = c.get_account_numbers()
            if acc_numbers_resp.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Schwab API returned {acc_numbers_resp.status_code}: "
                           f"{acc_numbers_resp.text[:200]}",
                )
            acc_entries = acc_numbers_resp.json() or []
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Schwab error fetching account list: {e}")

        accounts: list[dict] = []
        for entry in acc_entries:
            hash_value = entry.get("hashValue") or entry.get("accountHash")
            account_num = entry.get("accountNumber")
            if not hash_value:
                continue
            try:
                # schwab-py exposes the fields enum on the Client class
                pos_resp = c.get_account(hash_value, fields=Client.Account.Fields.POSITIONS)
                if pos_resp.status_code != 200:
                    log.warning("Schwab get_account(%s) returned %d: %s",
                                account_num, pos_resp.status_code, pos_resp.text[:200])
                    continue
                pos_data = pos_resp.json() or {}
            except Exception as e:
                log.warning("Schwab get_account(%s) failed: %s", account_num, e)
                continue

            sec_acc = pos_data.get("securitiesAccount", {}) or {}
            balances = sec_acc.get("currentBalances", {}) or {}
            initial_balances = sec_acc.get("initialBalances", {}) or {}
            positions = sec_acc.get("positions", []) or []

            # Some balance fields differ between cash and margin accounts; we
            # surface the common ones and skip ones that aren't present.
            def _g(name):
                return balances.get(name)

            accounts.append({
                "account_number": account_num,
                "account_hash": hash_value,
                "type": sec_acc.get("type"),
                "is_day_trader": sec_acc.get("isDayTrader", False),
                "round_trips": sec_acc.get("roundTrips", 0),
                "balances": {
                    "cash": _g("cashBalance"),
                    "equity": _g("equity"),
                    "liquidation_value": _g("liquidationValue"),
                    "buying_power": _g("buyingPower"),
                    "buying_power_non_margin": _g("buyingPowerNonMarginableTrade"),
                    "long_market_value": _g("longMarketValue"),
                    "short_market_value": _g("shortMarketValue"),
                    "available_funds": _g("availableFunds"),
                    "day_trading_buying_power": _g("dayTradingBuyingPower"),
                    "starting_equity": initial_balances.get("equity"),
                },
                "positions": [_format_schwab_position(p) for p in positions],
            })

        if not accounts:
            return {
                "accounts": [],
                "note": "No accounts returned from Schwab (token may be expired — re-run scripts/schwab_auth.py)",
                "disclaimer": STOCK_DISCLAIMER,
            }

        return {"accounts": accounts, "disclaimer": STOCK_DISCLAIMER}

    try:
        return await asyncio.wait_for(asyncio.to_thread(_do), timeout=30)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Schwab portfolio fetch timed out")


# ---------------------------------------------------------------------------
# Voice input (/api/transcribe) — faster-whisper STT
# ---------------------------------------------------------------------------

_whisper_model = None
WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "medium.en")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")


def _get_whisper():
    """Lazy-load the whisper model. Cached after first call."""
    global _whisper_model
    if _whisper_model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise HTTPException(status_code=500, detail=f"faster-whisper not installed: {e}")
        log.info("Loading faster-whisper model=%s device=%s compute=%s",
                 WHISPER_MODEL_NAME, WHISPER_DEVICE, WHISPER_COMPUTE)
        _whisper_model = WhisperModel(
            WHISPER_MODEL_NAME, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE
        )
        log.info("Whisper loaded.")
    return _whisper_model


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict[str, Any]:
    """Transcribe an uploaded audio blob using faster-whisper.

    Browser sends a webm/opus (or wav/mp3) blob from MediaRecorder; we
    write it to a temp file (faster-whisper takes a path) and stream
    segments back as one concatenated string. CPU int8 by default —
    medium.en transcribes ~5-10x realtime on Grace CPU.
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file uploaded")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty audio")
    if len(raw) > 100 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Audio too large (max 100 MB)")

    suffix = Path(file.filename).suffix or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    def _do() -> dict[str, Any]:
        m = _get_whisper()
        segments, info = m.transcribe(tmp_path, beam_size=5, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return {
            "text": text,
            "language": info.language,
            "duration_s": float(info.duration),
            "model": WHISPER_MODEL_NAME,
        }

    log.info("transcribe: %s (%d KB)", file.filename, len(raw) // 1024)
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(asyncio.to_thread(_do), timeout=300)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Transcription timed out")
    except Exception as e:
        log.exception("transcribe failed")
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    elapsed = time.monotonic() - t0
    log.info("transcribe: %.2fs of audio in %.2fs (%.1fx realtime), %d chars",
             result["duration_s"], elapsed,
             result["duration_s"] / max(elapsed, 0.001), len(result["text"]))
    return result


# ---------------------------------------------------------------------------
# Voice output (/api/tts) — Piper TTS
# ---------------------------------------------------------------------------

_piper_voice = None
PIPER_VOICE_PATH = os.getenv(
    "PIPER_VOICE",
    "/home/shiva/services/piper-voices/en_US-amy-medium.onnx",
)
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "5000"))


def _get_piper():
    """Lazy-load the Piper voice. Cached after first call."""
    global _piper_voice
    if _piper_voice is None:
        try:
            from piper import PiperVoice
        except ImportError as e:
            raise HTTPException(status_code=500, detail=f"piper-tts not installed: {e}")
        if not Path(PIPER_VOICE_PATH).exists():
            raise HTTPException(
                status_code=500,
                detail=f"Piper voice not found at {PIPER_VOICE_PATH}. "
                       "Set PIPER_VOICE env to the correct .onnx path.",
            )
        log.info("Loading Piper voice from %s", PIPER_VOICE_PATH)
        _piper_voice = PiperVoice.load(PIPER_VOICE_PATH)
        log.info("Piper voice loaded.")
    return _piper_voice


def _strip_markdown_for_tts(text: str) -> str:
    """Remove markdown characters / code blocks so TTS reads the words, not the syntax."""
    s = text or ""
    s = re.sub(r"```[\s\S]*?```", " (code block omitted) ", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"<details[^>]*>[\s\S]*?</details>", "", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


@app.post("/api/tts")
async def tts(req: Request) -> Response:
    """Synthesize speech from text using Piper. Returns audio/wav bytes."""
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text'")

    clean = _strip_markdown_for_tts(text)
    if not clean:
        raise HTTPException(status_code=400, detail="Nothing speakable in the input")
    if len(clean) > TTS_MAX_CHARS:
        clean = clean[:TTS_MAX_CHARS] + " ... (truncated for speech)"

    def _do() -> bytes:
        import wave
        voice = _get_piper()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            voice.synthesize(clean, wf)
        return buf.getvalue()

    log.info("tts: %d chars", len(clean))
    t0 = time.monotonic()
    try:
        audio = await asyncio.wait_for(asyncio.to_thread(_do), timeout=120)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="TTS timed out")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("tts failed")
        raise HTTPException(status_code=500, detail=f"{e.__class__.__name__}: {e}")
    log.info("tts: done in %.2fs (%d KB)", time.monotonic() - t0, len(audio) // 1024)
    return Response(content=audio, media_type="audio/wav",
                    headers={"Cache-Control": "no-cache"})


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


# ---------------------------------------------------------------------------
# Prompt history (/api/history)
#
# SQLite-backed log of every prompt the user has typed, so up/down arrow
# nav in the composer works across browsers, reloads, and devices (the
# server is the source of truth, the frontend just caches in RAM).
#
# Admin-gated like /api/state — shouldn't leak prompts to random LAN users.
# Stored in data/history.db. Schema is intentionally minimal; if/when we
# add multi-user, the next migration adds a user_id column.
# ---------------------------------------------------------------------------

HISTORY_DB_PATH = Path(os.getenv("HISTORY_DB_PATH", str(DATA_DIR / "history.db")))
HISTORY_MAX_ROWS = int(os.getenv("HISTORY_MAX_ROWS", "10000"))   # soft cap; we trim past this
HISTORY_MAX_TEXT_LEN = int(os.getenv("HISTORY_MAX_TEXT_LEN", "100000"))


def _history_conn() -> sqlite3.Connection:
    """Open the history DB, create schema on first call. One connection per
    call (cheap) — SQLite handles concurrency fine for our write rate."""
    conn = sqlite3.connect(HISTORY_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")    # better concurrent read/write
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            ts INTEGER NOT NULL,
            convo_id TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_history_ts ON prompt_history(ts DESC)")
    return conn


@app.post("/api/history")
async def history_append(req: Request) -> dict[str, Any]:
    """Append one prompt to history. Skips a consecutive duplicate of the
    most recent entry (bash HISTCONTROL=ignoredups behavior). Returns
    {"id": int, "skipped": False} or {"skipped": True}."""
    _check_auth(req)
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Missing 'text'")
    if len(text) > HISTORY_MAX_TEXT_LEN:
        raise HTTPException(status_code=413, detail="Text too long for history")
    convo_id = (body.get("convo_id") or None)

    def _do() -> dict[str, Any]:
        conn = _history_conn()
        try:
            last = conn.execute(
                "SELECT text FROM prompt_history ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last and last[0] == text:
                return {"skipped": True}
            cur = conn.execute(
                "INSERT INTO prompt_history (text, ts, convo_id) VALUES (?, ?, ?)",
                (text, int(time.time() * 1000), convo_id),
            )
            # Soft cap: trim oldest beyond HISTORY_MAX_ROWS.
            n = conn.execute("SELECT COUNT(*) FROM prompt_history").fetchone()[0]
            if n > HISTORY_MAX_ROWS:
                conn.execute(
                    "DELETE FROM prompt_history WHERE id IN ("
                    "  SELECT id FROM prompt_history ORDER BY id ASC LIMIT ?"
                    ")", (n - HISTORY_MAX_ROWS,)
                )
            conn.commit()
            return {"id": cur.lastrowid, "skipped": False}
        finally:
            conn.close()

    return await asyncio.to_thread(_do)


@app.get("/api/history")
async def history_list(req: Request) -> dict[str, Any]:
    """Return prompt history, newest first. Supports limit and optional
    substring search via ?q=."""
    _check_auth(req)
    try:
        limit = max(1, min(int(req.query_params.get("limit", "500")), 5000))
    except ValueError:
        limit = 500
    q = (req.query_params.get("q") or "").strip()

    def _do() -> dict[str, Any]:
        conn = _history_conn()
        try:
            if q:
                rows = conn.execute(
                    "SELECT id, text, ts, convo_id FROM prompt_history "
                    "WHERE text LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"%{q}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, text, ts, convo_id FROM prompt_history "
                    "ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return {
                "history": [
                    {"id": r[0], "text": r[1], "ts": r[2], "convo_id": r[3]}
                    for r in rows
                ],
                "count": len(rows),
            }
        finally:
            conn.close()

    return await asyncio.to_thread(_do)


@app.delete("/api/history/{entry_id}")
async def history_delete_one(entry_id: int, req: Request) -> dict[str, Any]:
    """Delete one history entry by id."""
    _check_auth(req)

    def _do() -> dict[str, Any]:
        conn = _history_conn()
        try:
            cur = conn.execute("DELETE FROM prompt_history WHERE id = ?", (entry_id,))
            conn.commit()
            return {"ok": True, "deleted": cur.rowcount}
        finally:
            conn.close()

    return await asyncio.to_thread(_do)


@app.delete("/api/history")
async def history_clear(req: Request) -> dict[str, Any]:
    """Wipe all history."""
    _check_auth(req)

    def _do() -> dict[str, Any]:
        conn = _history_conn()
        try:
            cur = conn.execute("DELETE FROM prompt_history")
            conn.commit()
            return {"ok": True, "deleted": cur.rowcount}
        finally:
            conn.close()

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Code Review (/api/codereview)
#
# Stream a code review from Ollama for a path that can be:
#   - a GitHub URL (file/blob, repo root, or tree/branch[/subdir])
#   - any other http(s) URL (single-file fetch, e.g. a gist raw URL)
#   - an SSH path "user@host:/path" (key-based auth only; uses `ssh` CLI)
#   - a local path on this host (the machine running the proxy, i.e. the DGX)
#
# Requires the admin token (same one /api/state uses), because this endpoint
# can read arbitrary filesystem paths and exec ssh. If you want it open,
# delete the `_check_auth(req)` line below — but only on a trusted LAN.
# ---------------------------------------------------------------------------

CODEREVIEW_MAX_FILES = int(os.getenv("CODEREVIEW_MAX_FILES", "30"))
CODEREVIEW_MAX_CHARS = int(os.getenv("CODEREVIEW_MAX_CHARS", "120000"))
CODEREVIEW_DEFAULT_MODEL = os.getenv("CODEREVIEW_DEFAULT_MODEL", "deepseek-coder-v2")

CODEREVIEW_FILE_EXTS = {
    ".py", ".pyx", ".pyi",
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".java", ".kt", ".kts", ".scala", ".groovy",
    ".go", ".rs",
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp",
    ".cs",
    ".rb", ".php",
    ".swift", ".m", ".mm",
    ".sh", ".bash", ".zsh", ".fish",
    ".lua", ".pl", ".r", ".jl", ".ex", ".exs", ".erl",
    ".sql",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".json", ".xml",
    ".md", ".rst", ".txt",
    ".tf", ".hcl", ".proto",
    ".gradle",
}
CODEREVIEW_NAMED_FILES = {
    "Makefile", "Dockerfile", "Rakefile", "Gemfile", "Procfile", "BUILD", "WORKSPACE",
}
CODEREVIEW_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".tox", "dist", "build", "target",
    ".idea", ".vscode", ".next", ".nuxt", ".output", "out",
    "coverage", "htmlcov", "vendor", "bower_components",
}

# user@host:/some/path or user@host:relative/path. user/host kept conservative.
_SSH_PATH_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)@([A-Za-z0-9.-]+):(.+)$")

# git@github.com:owner/repo[.git] — SSH-style git remote, not a real ssh fs path.
_GIT_SSH_GITHUB_RE = re.compile(r"^git@github\.com:([^/\s]+)/([^/\s]+?)(?:\.git)?$")


def _is_review_eligible(name_or_path: str) -> bool:
    name = Path(name_or_path).name
    if name in CODEREVIEW_NAMED_FILES:
        return True
    return Path(name_or_path).suffix.lower() in CODEREVIEW_FILE_EXTS


def _truncate_bundle(files: list[dict], max_chars: int) -> tuple[list[dict], bool]:
    total = 0
    out: list[dict] = []
    truncated = False
    for f in files:
        body = f.get("content") or ""
        if total + len(body) > max_chars:
            remaining = max_chars - total
            if remaining > 800:
                out.append({**f, "content": body[:remaining] + "\n\n... [truncated to fit budget]\n"})
            truncated = True
            break
        out.append(f)
        total += len(body)
    return out, truncated


async def _fetch_github(url: str) -> list[dict]:
    """Resolve a GitHub URL to a list of {path, content} entries."""
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="GitHub URL needs at least owner/repo")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "shivagpt-codereview",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with httpx.AsyncClient(timeout=20.0, headers=headers, follow_redirects=True) as cli:
        # Case 1: blob URL → single file
        if len(parts) >= 5 and parts[2] == "blob":
            branch = parts[3]
            file_path = "/".join(parts[4:])
            raw = await cli.get(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
            )
            if raw.status_code != 200:
                raise HTTPException(status_code=raw.status_code,
                                    detail=f"Could not fetch raw file: {raw.text[:200]}")
            return [{"path": f"{owner}/{repo}/{file_path}", "content": raw.text}]

        # Case 2: tree URL or repo root
        if len(parts) >= 4 and parts[2] == "tree":
            branch = parts[3]
            subdir = "/".join(parts[4:]).rstrip("/")
        else:
            # Look up default branch
            r = await cli.get(f"https://api.github.com/repos/{owner}/{repo}")
            if r.status_code == 404:
                raise HTTPException(status_code=404, detail=f"GitHub repo {owner}/{repo} not found")
            r.raise_for_status()
            branch = r.json().get("default_branch", "main")
            subdir = ""

        r = await cli.get(
            f"https://api.github.com/repos/{owner}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        if r.status_code == 404:
            raise HTTPException(status_code=404,
                                detail=f"Branch '{branch}' not found in {owner}/{repo}")
        r.raise_for_status()
        payload = r.json()
        if payload.get("truncated"):
            log.warning("codereview: GitHub tree response was truncated for %s/%s@%s",
                        owner, repo, branch)
        tree = payload.get("tree", [])

        candidates: list[str] = []
        for entry in tree:
            if entry.get("type") != "blob":
                continue
            p = entry.get("path", "")
            if subdir and not (p == subdir or p.startswith(subdir + "/")):
                continue
            if any(seg in CODEREVIEW_SKIP_DIRS for seg in p.split("/")):
                continue
            if not _is_review_eligible(p):
                continue
            candidates.append(p)

        if not candidates:
            raise HTTPException(status_code=400,
                                detail="No reviewable source files found at that GitHub URL")
        candidates = candidates[:CODEREVIEW_MAX_FILES]

        async def fetch_one(p: str) -> dict | None:
            raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{p}"
            rr = await cli.get(raw_url)
            if rr.status_code != 200:
                return None
            return {"path": f"{owner}/{repo}/{p}", "content": rr.text}

        results = await asyncio.gather(*[fetch_one(p) for p in candidates])
        return [r for r in results if r]


async def _fetch_url(url: str) -> list[dict]:
    """Fetch any other URL as one text blob."""
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                 headers={"User-Agent": "shivagpt-codereview"}) as cli:
        r = await cli.get(url)
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code,
                                detail=f"Could not fetch {url}: {r.text[:200]}")
        name = Path(urlparse(url).path).name or "remote"
        return [{"path": name, "content": r.text}]


def _walk_local(root: Path) -> list[dict]:
    if not root.exists():
        raise HTTPException(status_code=404, detail=f"Local path not found: {root}")
    if root.is_file():
        try:
            return [{"path": str(root), "content": root.read_text(encoding="utf-8", errors="replace")}]
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"Cannot read {root}: {e}")
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a file or directory: {root}")
    out: list[dict] = []
    for cur, dirs, names in os.walk(root):
        # Prune in-place so os.walk skips entire subtrees
        dirs[:] = [d for d in dirs if d not in CODEREVIEW_SKIP_DIRS and not d.startswith(".")]
        for n in names:
            p = Path(cur) / n
            if not _is_review_eligible(str(p)):
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            try:
                rel = p.relative_to(root.parent)
            except ValueError:
                rel = p
            out.append({"path": str(rel), "content": content})
            if len(out) >= CODEREVIEW_MAX_FILES:
                return out
    return out


def _fetch_ssh(user: str, host: str, remote_path: str) -> list[dict]:
    """Pull files from a remote host via the local `ssh` binary.

    Uses BatchMode=yes — keys only, no password prompts. Enumerates first
    with `find`, then concatenates a small number of files in one ssh round
    trip using a unique marker so we can split the output back into files.
    """
    target = f"{user}@{host}"

    # 1. Enumerate eligible files on the remote. Find with prune for noise dirs
    #    and an OR list of -iname patterns for the extensions we care about.
    name_clause = " -o ".join(f"-iname '*{ext}'" for ext in sorted(CODEREVIEW_FILE_EXTS))
    name_clause += "".join(f" -o -name '{n}'" for n in sorted(CODEREVIEW_NAMED_FILES))
    prune_clause = " -o ".join(f"-name '{d}'" for d in sorted(CODEREVIEW_SKIP_DIRS))

    quoted_path = shlex.quote(remote_path)
    enum_script = (
        f"if [ -f {quoted_path} ]; then echo F:{quoted_path}; exit 0; fi; "
        f"if [ ! -d {quoted_path} ]; then echo MISSING; exit 0; fi; "
        f"find {quoted_path} \\( -type d \\( {prune_clause} \\) -prune \\) -o "
        f"-type f \\( {name_clause} \\) -print | head -n {CODEREVIEW_MAX_FILES}"
    )

    try:
        listing = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=accept-new", target, enum_script],
            capture_output=True, text=True, timeout=25,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504,
                            detail=f"SSH to {target} timed out during enumeration")
    if listing.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=f"SSH to {target} failed: {listing.stderr.strip()[:300] or 'unknown error'}",
        )
    stdout = listing.stdout.strip()
    if not stdout or stdout == "MISSING":
        raise HTTPException(status_code=404,
                            detail=f"Remote path not found: {target}:{remote_path}")

    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if lines and lines[0].startswith("F:"):
        paths = [lines[0][2:]]
    else:
        paths = lines[:CODEREVIEW_MAX_FILES]

    if not paths:
        raise HTTPException(status_code=400,
                            detail="No reviewable source files found on the remote path")

    # 2. Cat all chosen files in one ssh call using a unique delimiter.
    marker = f"---FILE-{secrets.token_hex(6)}---"
    cat_script_parts = []
    for p in paths:
        qp = shlex.quote(p)
        cat_script_parts.append(f"printf '%s\\n%s\\n' {shlex.quote(marker)} {qp}; cat {qp}")
    cat_script = "; ".join(cat_script_parts)

    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
             "-o", "StrictHostKeyChecking=accept-new", target, cat_script],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504,
                            detail=f"SSH to {target} timed out while reading files")
    if result.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=f"SSH cat failed on {target}: {result.stderr.strip()[:300]}",
        )

    out: list[dict] = []
    chunks = result.stdout.split(marker + "\n")
    for chunk in chunks:
        if not chunk:
            continue
        nl = chunk.find("\n")
        if nl < 0:
            continue
        path_line = chunk[:nl]
        body = chunk[nl + 1:]
        out.append({"path": f"{target}:{path_line}", "content": body})
    if not out:
        raise HTTPException(status_code=502,
                            detail="Could not parse any files from the SSH response")
    return out


def _build_codereview_prompt(files: list[dict], instructions: str) -> tuple[str, str]:
    files, truncated = _truncate_bundle(files, CODEREVIEW_MAX_CHARS)
    body_parts = []
    for f in files:
        body_parts.append(f"\n### `{f['path']}`\n```\n{f['content']}\n```\n")
    system = (
        "You are a senior software engineer doing a focused code review. "
        "Be concrete: point to specific functions, line patterns, or variables; "
        "skip nitpicks unless they materially affect correctness or maintainability. "
        "Group findings by file. When you suggest a change, show a short fenced code "
        "block with the proposed rewrite. End with: (1) a short overall summary and "
        "(2) the top three changes you would make."
    )
    instr_block = f"{instructions.strip()}\n\n" if instructions.strip() else ""
    user = (
        f"{instr_block}Please review the following {len(files)} file(s)."
        + ("\n\n[NOTE: the file bundle was truncated to stay within budget.]" if truncated else "")
        + "".join(body_parts)
    )
    return system, user


@app.post("/api/codereview")
async def code_review(req: Request) -> StreamingResponse:
    """Gather code from `path` (GitHub URL / other URL / SSH / local) and
    stream a review from Ollama as NDJSON, same shape as /api/chat."""
    _check_auth(req)

    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="Missing 'path'")
    model = (body.get("model") or "").strip() or CODEREVIEW_DEFAULT_MODEL
    instructions = (body.get("instructions") or "").strip()
    try:
        temperature = float(body.get("temperature", 0.2))
    except (TypeError, ValueError):
        temperature = 0.2

    # Resolve path → list of files. Order matters: the git-SSH-remote pattern
    # for GitHub overlaps the generic user@host:path pattern, so check it first.
    try:
        gh_ssh = _GIT_SSH_GITHUB_RE.match(path)
        if gh_ssh:
            owner, repo = gh_ssh.group(1), gh_ssh.group(2)
            files = await _fetch_github(f"https://github.com/{owner}/{repo}")
        elif path.startswith(("https://github.com/", "http://github.com/")):
            files = await _fetch_github(path)
        elif path.startswith(("http://", "https://")):
            files = await _fetch_url(path)
        elif _SSH_PATH_RE.match(path):
            m = _SSH_PATH_RE.match(path)
            assert m is not None
            files = await asyncio.to_thread(_fetch_ssh, m.group(1), m.group(2), m.group(3))
        else:
            files = await asyncio.to_thread(_walk_local, Path(path).expanduser())
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")
    except Exception as e:
        log.exception("codereview: gather failed for %s", path)
        raise HTTPException(status_code=500,
                            detail=f"Failed to gather files: {e.__class__.__name__}: {e}")

    if not files:
        raise HTTPException(status_code=400, detail="No reviewable files found at that path")

    system_prompt, user_prompt = _build_codereview_prompt(files, instructions)
    total_chars = sum(len(f["content"]) for f in files)
    log.info("codereview: path=%r files=%d chars=%d model=%s",
             path, len(files), total_chars, model)

    upstream = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": True,
        "options": {"temperature": temperature},
    }).encode("utf-8")

    file_list_preview = "\n".join(f"  - `{f['path']}`" for f in files[:20])
    if len(files) > 20:
        file_list_preview += f"\n  - … and {len(files) - 20} more"
    preamble = (
        f"_Gathered **{len(files)}** file(s) ({total_chars:,} chars) from `{path}`._\n"
        f"_Model: `{model}`._\n\n"
        f"<details><summary>Files reviewed</summary>\n\n{file_list_preview}\n\n</details>\n\n"
    )

    async def streamer():
        # Emit a small preamble in the same NDJSON shape Ollama uses.
        yield (json.dumps({"message": {"role": "assistant", "content": preamble}}) + "\n").encode()
        timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                async with c.stream(
                    "POST",
                    f"{OLLAMA_URL}/api/chat",
                    content=upstream,
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
        except httpx.ConnectError:
            yield (json.dumps({"error": f"Cannot connect to Ollama at {OLLAMA_URL}."}) + "\n").encode()
        except httpx.ReadTimeout:
            yield (json.dumps({"error": "Ollama timed out while generating."}) + "\n").encode()
        except Exception as e:
            log.exception("codereview stream failed")
            yield (json.dumps({"error": f"Server error: {e.__class__.__name__}: {e}"}) + "\n").encode()

    return StreamingResponse(
        streamer(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Web search & URL fetch (/api/search, /api/fetch)
#
# /api/search: query SearXNG, optionally fetch the top-N pages' main text,
#   then stream a cited answer from Ollama (same NDJSON shape as /api/chat).
# /api/fetch:  pull one URL, extract main article text, and stream a model
#   response that uses it as context.
#
# Both are admin-gated, matching /codereview, because /fetch can be aimed
# at internal LAN URLs and /search inherits the same surface.
# ---------------------------------------------------------------------------

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8888").rstrip("/")
SEARCH_DEFAULT_RESULTS = int(os.getenv("SEARCH_DEFAULT_RESULTS", "6"))
SEARCH_DEFAULT_FETCH = int(os.getenv("SEARCH_DEFAULT_FETCH", "3"))
SEARCH_FETCH_MAX_CHARS = int(os.getenv("SEARCH_FETCH_MAX_CHARS", "8000"))   # per page
FETCH_MAX_CHARS = int(os.getenv("FETCH_MAX_CHARS", "40000"))                 # /api/fetch single page cap
FETCH_TIMEOUT_S = float(os.getenv("FETCH_TIMEOUT_S", "15"))


def _extract_main_text(html: str) -> str:
    """Pull the main article text out of an HTML page.

    Tries trafilatura first (best quality); falls back to a crude tag-strip
    so the endpoint still works if trafilatura isn't installed.
    """
    try:
        import trafilatura  # type: ignore
        out = trafilatura.extract(
            html,
            include_links=False,
            include_images=False,
            include_tables=False,
            favor_recall=True,
            no_fallback=False,
        ) or ""
        if out.strip():
            return out
    except Exception as e:  # pragma: no cover
        log.debug("trafilatura unavailable or failed: %s", e)

    # Fallback: kill <script>/<style>, strip tags, collapse whitespace.
    cleaned = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


async def _searxng_search(query: str, num: int) -> list[dict]:
    """Returns [{title, url, snippet, engine}] from the SearXNG JSON API."""
    params = {"q": query, "format": "json", "safesearch": "0"}
    async with httpx.AsyncClient(timeout=20.0,
                                 headers={"User-Agent": "shivagpt-search/1.0"}) as cli:
        try:
            r = await cli.get(f"{SEARXNG_URL}/search", params=params)
        except httpx.ConnectError as e:
            raise HTTPException(status_code=502,
                                detail=f"Cannot reach SearXNG at {SEARXNG_URL}: {e}")
        if r.status_code != 200:
            raise HTTPException(status_code=502,
                                detail=f"SearXNG returned {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except Exception:
            raise HTTPException(status_code=502,
                                detail="SearXNG didn't return JSON — is the json format enabled "
                                       "in /etc/searxng/settings.yml?")
    results = []
    for item in (data.get("results") or [])[:num]:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": (item.get("title") or url).strip(),
            "url": url,
            "snippet": (item.get("content") or "").strip(),
            "engine": item.get("engine", ""),
        })
    return results


async def _fetch_one(url: str, max_chars: int) -> tuple[str, str]:
    """Returns (title, text). Title comes from <title>; text is main content.

    Uses a real-browser UA because many sites (WhitePages, LinkedIn, news
    sites behind anti-scraper services) reject obviously non-browser
    requests with 403/999. Override with FETCH_USER_AGENT env if you want
    something more honest at the cost of getting blocked more often.
    """
    ua = os.getenv(
        "FETCH_USER_AGENT",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2_1) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    )
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    ) as cli:
        r = await cli.get(url)
        r.raise_for_status()
        # Skip obviously non-HTML responses; the model can't do much with bytes.
        ctype = (r.headers.get("content-type") or "").lower()
        if any(t in ctype for t in ("image/", "audio/", "video/", "application/pdf",
                                     "application/zip", "application/octet-stream")):
            raise HTTPException(status_code=415,
                                detail=f"Cannot extract text from content-type: {ctype}")
        body = r.text
    # title
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", body)
    title = re.sub(r"\s+", " ", m.group(1)).strip() if m else url
    text = await asyncio.to_thread(_extract_main_text, body)
    if not text:
        raise HTTPException(status_code=502,
                            detail=f"Could not extract any text from {url}")
    return title, text[:max_chars]


def _build_search_prompt(query: str, results: list[dict],
                         pages: list[dict], instructions: str) -> tuple[str, str]:
    """Compose a (system, user) prompt that grounds the answer in search results."""
    system = (
        "You are a careful research assistant. Use the search results below to "
        "answer the user's question. Cite each claim that comes from a result "
        "using bracketed numbers like [1], [2] that match the result list. If "
        "the results disagree or don't actually answer the question, say so "
        "openly. Don't invent facts that aren't in the results. End with a "
        "one-line 'Sources:' section that lists [n] title — url for every "
        "citation you used."
    )
    parts = [f"# Question\n{query}\n"]
    if instructions:
        parts.append(f"# Additional instructions\n{instructions}\n")
    parts.append("# Search results")
    for i, r in enumerate(results, 1):
        parts.append(f"\n[{i}] **{r['title']}** — {r['url']}")
        if r.get("snippet"):
            parts.append(f"    {r['snippet']}")
    if pages:
        parts.append("\n# Full text of top results")
        for p in pages:
            parts.append(f"\n## [{p['index']}] {p['title']}\nURL: {p['url']}\n\n{p['text']}\n")
    user = "\n".join(parts)
    return system, user


async def _stream_ollama_chat(model: str, system: str, user: str,
                               temperature: float, preamble: str):
    """Generic NDJSON streamer: preamble first, then Ollama's tokens, in the
    /api/chat shape so the frontend reader can be reused unchanged.

    Also emits a leading {"meta": {"model": ...}} line so the frontend can
    update the assistant message badge to reflect what model actually ran
    server-side, rather than guessing from the conversation's current model.

    Yields bytes ready to push into a StreamingResponse.
    """
    upstream = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": True,
        "options": {"temperature": temperature},
    }).encode("utf-8")

    # Tell the frontend what model actually ran (not what the convo's set to).
    yield (json.dumps({"meta": {"model": model}}) + "\n").encode()
    if preamble:
        yield (json.dumps({"message": {"role": "assistant",
                                        "content": preamble}}) + "\n").encode()
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            async with c.stream("POST", f"{OLLAMA_URL}/api/chat",
                                content=upstream,
                                headers={"content-type": "application/json"}) as r:
                if r.status_code != 200:
                    text = await r.aread()
                    msg = text.decode("utf-8", errors="replace")
                    yield (json.dumps({"error": msg, "status": r.status_code}) + "\n").encode()
                    return
                async for chunk in r.aiter_raw():
                    if chunk:
                        yield chunk
    except httpx.ConnectError:
        yield (json.dumps({"error": f"Cannot connect to Ollama at {OLLAMA_URL}."}) + "\n").encode()
    except httpx.ReadTimeout:
        yield (json.dumps({"error": "Ollama timed out while generating."}) + "\n").encode()
    except Exception as e:
        log.exception("ollama stream failed")
        yield (json.dumps({"error": f"Server error: {e.__class__.__name__}: {e}"}) + "\n").encode()


@app.post("/api/search")
async def web_search(req: Request) -> StreamingResponse:
    """Search via SearXNG, fetch top-N page texts, stream a cited answer."""
    _check_auth(req)
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="Missing 'query'")

    model = (body.get("model") or "").strip() or os.getenv("SEARCH_DEFAULT_MODEL", "llama3.3")
    instructions = (body.get("instructions") or "").strip()
    num_results = max(1, min(int(body.get("num_results") or SEARCH_DEFAULT_RESULTS), 15))
    fetch_top = max(0, min(int(body.get("fetch_top") or SEARCH_DEFAULT_FETCH), num_results))
    try:
        temperature = float(body.get("temperature", 0.3))
    except (TypeError, ValueError):
        temperature = 0.3

    log.info("search: q=%r num=%d fetch_top=%d model=%s", query, num_results, fetch_top, model)

    # 1. Get search results
    results = await _searxng_search(query, num_results)
    if not results:
        raise HTTPException(status_code=502,
                            detail=f"SearXNG returned no results for {query!r}")

    # 2. Fetch the top-N page texts (best-effort; failures get surfaced in the
    #    preamble so the user knows when the answer is snippet-only).
    pages: list[dict] = []
    fetch_outcomes: list[dict] = []  # one per attempted URL
    if fetch_top > 0:
        async def _maybe_fetch(i: int, r: dict) -> dict:
            try:
                title, text = await _fetch_one(r["url"], SEARCH_FETCH_MAX_CHARS)
                return {"i": i, "url": r["url"], "title": title or r["title"],
                        "ok": True, "text": text}
            except HTTPException as e:
                # Our own exception with a clean detail (e.g. 415 content-type)
                return {"i": i, "url": r["url"], "title": r["title"],
                        "ok": False, "reason": f"{e.status_code}: {e.detail[:80]}"}
            except httpx.HTTPStatusError as e:
                code = e.response.status_code
                label = {403: "blocked (403)", 999: "blocked (LinkedIn 999)",
                         429: "rate-limited (429)", 401: "auth required (401)"}.get(
                    code, f"HTTP {code}")
                return {"i": i, "url": r["url"], "title": r["title"],
                        "ok": False, "reason": label}
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                return {"i": i, "url": r["url"], "title": r["title"],
                        "ok": False, "reason": "timed out"}
            except Exception as e:
                return {"i": i, "url": r["url"], "title": r["title"],
                        "ok": False, "reason": f"{e.__class__.__name__}: {str(e)[:60]}"}
        fetch_outcomes = await asyncio.gather(
            *[_maybe_fetch(i + 1, r) for i, r in enumerate(results[:fetch_top])]
        )
        for o in fetch_outcomes:
            if o["ok"]:
                pages.append({"index": o["i"], "title": o["title"],
                              "url": o["url"], "text": o["text"]})
            else:
                log.debug("search: skip fetch %s: %s", o["url"], o["reason"])

    system, user = _build_search_prompt(query, results, pages, instructions)

    # Preamble shows the user what was found before any tokens stream in,
    # marking each top-fetch result with a ✓ (got page text) or ✗ (blocked /
    # empty / timed out) plus reason so they know when an answer is
    # snippet-only.
    outcome_by_idx = {o["i"]: o for o in fetch_outcomes}  # 1-indexed
    def _line(i: int, r: dict) -> str:
        idx = i + 1
        o = outcome_by_idx.get(idx)
        if o is None:
            marker = ""                       # not in fetch_top window
        elif o["ok"]:
            marker = " ✓"
        else:
            marker = f" ✗ _{o['reason']}_"
        engine = f"  · _{r['engine']}_" if r.get("engine") else ""
        return f"  [{idx}] [{r['title']}]({r['url']}){engine}{marker}"
    preview_lines = "\n".join(_line(i, r) for i, r in enumerate(results))

    n_ok = len(pages)
    n_fail = sum(1 for o in fetch_outcomes if not o["ok"])
    if n_ok and n_fail:
        fetched_note = (f"_Got full text from **{n_ok}** result(s); **{n_fail}** "
                        f"blocked or empty (using snippets for those)._\n\n")
    elif n_ok:
        fetched_note = f"_Got full text from **{n_ok}** result(s)._\n\n"
    elif n_fail:
        fetched_note = (f"_All **{n_fail}** top result(s) were blocked/empty — "
                        f"answering from snippets only._\n\n")
    else:
        fetched_note = "_Snippets-only mode (no full-text fetch)._\n\n"

    preamble = (
        f"_Searching for **{query}** via SearXNG · model `{model}`…_\n\n"
        f"<details><summary>Search results</summary>\n\n{preview_lines}\n\n</details>\n\n"
        f"{fetched_note}"
    )

    return StreamingResponse(
        _stream_ollama_chat(model, system, user, temperature, preamble),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/fetch")
async def fetch_url(req: Request) -> StreamingResponse:
    """Fetch a single URL, extract main text, stream a model response."""
    _check_auth(req)
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    url = (body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url'")
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL must start with http:// or https://")

    model = (body.get("model") or "").strip() or os.getenv("SEARCH_DEFAULT_MODEL", "llama3.3")
    instructions = (body.get("instructions") or "").strip()
    try:
        temperature = float(body.get("temperature", 0.3))
    except (TypeError, ValueError):
        temperature = 0.3

    log.info("fetch: url=%s model=%s", url, model)

    try:
        title, text = await _fetch_one(url, FETCH_MAX_CHARS)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Fetch failed: {e}")
    except HTTPException:
        raise
    except Exception as e:
        log.exception("fetch: unexpected error")
        raise HTTPException(status_code=502, detail=f"Fetch failed: {e.__class__.__name__}: {e}")

    system = (
        "You are reading a single web page on the user's behalf. Answer their "
        "question using only the page content provided. If the answer isn't in "
        "the page, say so. Quote sparingly and accurately."
    )
    default_instr = "Summarize this page in clear bullet points."
    user_q = instructions or default_instr
    user = (
        f"# URL\n{url}\n\n# Title\n{title}\n\n# Page content\n{text}\n\n"
        f"# Question\n{user_q}"
    )

    preamble = (
        f"_Fetched **{len(text):,}** chars from [{title}]({url}) · model `{model}`._\n\n"
    )

    return StreamingResponse(
        _stream_ollama_chat(model, system, user, temperature, preamble),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
