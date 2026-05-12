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

# Global for diffusers lazy-loading
_imgen_pipeline = None

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
# Image Generation (/api/imgen)
# ---------------------------------------------------------------------------

def _get_imgen_pipeline():
    global _imgen_pipeline
    if _imgen_pipeline is None:
        log.info("Loading diffusers pipeline (this will take a while)...")
        import torch
        from diffusers import AutoPipelineForText2Image
        # Full SDXL Base 1.0 for high quality
        _imgen_pipeline = AutoPipelineForText2Image.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            torch_dtype=torch.float16,
            variant="fp16",
            use_safetensors=True
        )
        _imgen_pipeline = _imgen_pipeline.to("cuda")
        log.info("Pipeline loaded.")
    return _imgen_pipeline

@app.post("/api/imgen")
async def process_imgen(req: Request) -> dict[str, Any]:
    try:
        body = await req.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    prompt = body.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Missing 'prompt'")
        
    log.info("Generating image for prompt: %s", prompt)
    
    def _generate(p: str):
        pipe = _get_imgen_pipeline()
        # SDXL Base requires standard steps and resolution
        neg_prompt = "ugly, deformed, extra limbs, extra fingers, bad anatomy, blurry, worst quality, low resolution, jpeg artifacts"
        image = pipe(
            prompt=p, 
            negative_prompt=neg_prompt,
            num_inference_steps=25, 
            guidance_scale=7.5,
            width=1024,
            height=1024
        ).images[0]
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=92)
        return out.getvalue()
        
    try:
        result_bytes = await asyncio.wait_for(
            asyncio.to_thread(_generate, prompt),
            timeout=300.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Image generation timed out")
    except Exception as e:
        log.exception("Image generation failed")
        raise HTTPException(status_code=500, detail=str(e))
        
    b64_out = base64.b64encode(result_bytes).decode("ascii")
    
    return {
        "image": b64_out,
        "mime": "image/jpeg"
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
    """Returns (title, text). Title comes from <title>; text is main content."""
    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_S,
        follow_redirects=True,
        headers={"User-Agent": "shivagpt-fetch/1.0 (+private LAN assistant)"},
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

    # 2. Fetch the top-N page texts (best-effort; failures are skipped silently)
    pages: list[dict] = []
    if fetch_top > 0:
        async def _maybe_fetch(i: int, r: dict):
            try:
                title, text = await _fetch_one(r["url"], SEARCH_FETCH_MAX_CHARS)
                return {"index": i, "title": title or r["title"], "url": r["url"], "text": text}
            except Exception as e:
                log.debug("search: skip fetch %s: %s", r["url"], e)
                return None
        fetched = await asyncio.gather(*[_maybe_fetch(i + 1, r) for i, r in enumerate(results[:fetch_top])])
        pages = [p for p in fetched if p]

    system, user = _build_search_prompt(query, results, pages, instructions)

    # Preamble shows the user what was found before any tokens stream in.
    preview_lines = "\n".join(
        f"  [{i+1}] [{r['title']}]({r['url']})" + (f"  · _{r['engine']}_" if r.get("engine") else "")
        for i, r in enumerate(results)
    )
    fetched_note = (f"_Fetched full text of **{len(pages)}** result(s)._\n\n"
                    if pages else "_No full-text fetch; using snippets only._\n\n")
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
