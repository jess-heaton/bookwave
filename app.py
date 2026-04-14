import asyncio, os, re, socket, uuid, time, threading, webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiosqlite
try:
    import pymupdf as fitz
except ImportError:
    import fitz

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ── Paths ─────────────────────────────────────────────────────────────────────
# Storage lives OUTSIDE the project dir so OneDrive doesn't sync audio files.
# Override with env var BOOKWAVE_STORAGE=C:\path\to\storage
BASE   = Path(__file__).parent
STATIC = BASE / "static"
_default_store = Path(os.environ.get("LOCALAPPDATA", str(BASE))) / "Bookwave"
STORE  = Path(os.environ.get("BOOKWAVE_STORAGE", str(_default_store)))
COVERS = STORE / "covers"
AUDIO  = STORE / "audio"
UPLOADS= STORE / "uploads"
DB     = STORE / "books.db"
print(f"[STORAGE] {STORE}")

for d in (STATIC, COVERS, AUDIO, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/covers", StaticFiles(directory=str(COVERS)), name="covers")
app.mount("/audio",  StaticFiles(directory=str(AUDIO)),  name="audio")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

progress: dict = {}

# ── DB ────────────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS books (
            id TEXT PRIMARY KEY, title TEXT, author TEXT DEFAULT '',
            cover TEXT DEFAULT '', total INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0, status TEXT DEFAULT 'uploaded',
            voice TEXT DEFAULT 'af_bella', created REAL)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS chapters (
            id TEXT PRIMARY KEY, book_id TEXT, num INTEGER,
            title TEXT, words INTEGER DEFAULT 0,
            audio TEXT DEFAULT '', status TEXT DEFAULT 'pending')""")
        await db.execute("""CREATE TABLE IF NOT EXISTS texts (
            id TEXT PRIMARY KEY, text TEXT)""")
        await db.commit()

@app.on_event("startup")
async def startup(): await init_db()

# ── PDF helpers ───────────────────────────────────────────────────────────────
CHAP = re.compile(
    r"^(chapter|part|book|section|prologue|epilogue|introduction|preface|afterword)"
    r"(\s+(\d+|[ivxlcdm]+|[a-z\-]+))?\s*[:\-–—]?\s*(.*)$", re.IGNORECASE)

def split_chapters(text):
    lines, chapters = text.split("\n"), []
    title, buf, found = "Beginning", [], False
    for line in lines:
        s = line.strip()
        m = CHAP.match(s) if s and len(s) < 120 else None
        if m:
            body = "\n".join(buf).strip()
            if len(body) > 80:
                chapters.append({"title": title, "text": body})
                found = True
            parts = [m.group(1).title()]
            if m.group(3): parts.append(m.group(3))
            if m.group(4): parts.append(m.group(4).strip())
            title, buf = " ".join(parts), []
        else:
            buf.append(line)
    body = "\n".join(buf).strip()
    if len(body) > 80:
        chapters.append({"title": title, "text": body})
    if not found or len(chapters) == 1:
        words = text.split()
        chapters = [{"title": f"Part {i+1}", "text": " ".join(words[i*2500:(i+1)*2500])}
                    for i in range(max(1, len(words)//2500 + 1)) if words[i*2500:(i+1)*2500]]
    return chapters

# ── TTS (Kokoro — high quality neural voices) ────────────────────────────────
# If USE_MODAL=1, offload generation to a GPU on Modal.com (~50x faster).
USE_MODAL = os.environ.get("USE_MODAL") == "1"
_tts_executor = ThreadPoolExecutor(max_workers=1)
_pipeline: dict = {}  # lang_code → KPipeline, lazy-loaded (local fallback)
_modal_fn = None

def _get_modal_fn():
    global _modal_fn
    if _modal_fn is None:
        import modal
        _modal_fn = modal.Function.from_name("bookwave-tts", "kokoro_tts")
    return _modal_fn

KOKORO_VOICES = [
    ("af_heart",   "Heart — US Female (warm, natural)"),
    ("af_bella",   "Bella — US Female (bright)"),
    ("af_nicole",  "Nicole — US Female (calm)"),
    ("af_sarah",   "Sarah — US Female (clear)"),
    ("am_adam",    "Adam — US Male (deep)"),
    ("am_michael", "Michael — US Male (rich)"),
    ("bf_emma",    "Emma — British Female"),
    ("bf_isabella","Isabella — British Female"),
    ("bm_george",  "George — British Male"),
    ("bm_lewis",   "Lewis — British Male"),
]

def _lang_for_voice(voice: str) -> str:
    return "b" if voice.startswith("b") else "a"

def clean_text(text):
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', ' ', text)
    text = re.sub(r'[^\S\n]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.replace('\u2019', "'").replace('\u2018', "'")
    text = text.replace('\u201c', '"').replace('\u201d', '"')
    text = text.replace('\u2013', '-').replace('\u2014', ' - ')
    text = text.replace('\u2026', '...')
    return text.strip()

# Boilerplate phrases that appear on copyright/front-matter pages — skip them
_SKIP_PHRASES = [
    "all rights reserved", "without permission", "no part of this",
    "published by", "printed in", "library of congress", "isbn",
    "first published", "copyright ©", "penguin", "random house",
]

def is_boilerplate(text: str) -> bool:
    low = text.lower()
    hits = sum(1 for p in _SKIP_PHRASES if p in low)
    words = len(text.split())
    return words < 80 or hits >= 3

def scrub_text(text: str) -> str:
    """Extra cleanup before sending to Kokoro's phonemizer."""
    # Remove URLs which can hang the phonemizer
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'www\.\S+', '', text)
    # Break apart tokens longer than 40 chars (e.g. dashes run together)
    text = re.sub(r'(\S{40,})', lambda m: ' '.join(m.group(0)[i:i+20] for i in range(0, len(m.group(0)), 20)), text)
    # Remove lines that are pure numbers / codes (page numbers, ISBNs etc.)
    lines = [l for l in text.split('\n') if not re.fullmatch(r'[\d\s\-\.,:;]+', l.strip())]
    text = '\n'.join(lines)
    # Collapse excessive whitespace again
    text = re.sub(r'[^\S\n]+', ' ', text).strip()
    return text

def _run_kokoro_modal(text: str, voice: str, out_path: str):
    print("[TTS] Calling Modal GPU…", flush=True)
    t0 = time.time()
    data = _get_modal_fn().remote(text, voice)
    Path(out_path).write_bytes(data)
    print(f"[TTS] Modal done in {time.time()-t0:.1f}s — {len(data):,} bytes", flush=True)

def _run_kokoro(text: str, voice: str, out_path: str):
    if USE_MODAL:
        return _run_kokoro_modal(text, voice, out_path)
    # Local CPU fallback — imports done lazily so cloud deploys don't need these libs.
    import numpy as np
    import soundfile as sf
    from kokoro import KPipeline
    lang = _lang_for_voice(voice)
    if lang not in _pipeline:
        print("[TTS] Loading Kokoro model (first run — downloading ~300MB)…")
        _pipeline[lang] = KPipeline(lang_code=lang)
        print("[TTS] Model ready.")
    pipe = _pipeline[lang]
    chunks = []
    words = len(text.split())
    print(f"[TTS] {words} words → generating audio...", flush=True)
    for i, (_, _, audio) in enumerate(pipe(text, voice=voice, speed=1.0)):
        chunks.append(audio)
        print(f"[TTS]   sentence {i+1} ✓", end='\r', flush=True)
    print(f"\n[TTS] Done — {len(chunks)} sentences", flush=True)
    if not chunks:
        raise RuntimeError("Kokoro returned no audio")
    sf.write(out_path, np.concatenate(chunks), 24000)

async def tts_chapter(chapter_id, text, voice):
    text = clean_text(text)
    if not text or is_boilerplate(text):
        return None

    text = scrub_text(text)
    if len(text.split()) < 20:
        return None

    ext = "mp3" if USE_MODAL else "wav"
    out = str(AUDIO / f"{chapter_id}.{ext}")
    try:
        await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                _tts_executor, _run_kokoro, text, voice, out
            ),
            timeout=1800,  # 30 min — Kokoro on CPU can be slow for long chapters
        )
    except asyncio.TimeoutError:
        raise RuntimeError("Chapter timed out after 30 min")

    if not Path(out).exists() or Path(out).stat().st_size < 100:
        raise RuntimeError("TTS produced no audio")
    return f"/audio/{chapter_id}.{ext}"

async def generate_book(book_id, voice_id):
    print(f"\n[GEN] Starting book {book_id}")
    progress[book_id] = {"done": 0, "total": 0, "status": "generating"}

    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id,title FROM chapters WHERE book_id=? ORDER BY num", (book_id,)) as c:
            chapters = await c.fetchall()

    progress[book_id]["total"] = len(chapters)
    errors = 0

    for ch in chapters:
        cid, ctitle = ch["id"], ch["title"]
        print(f"[GEN] {ctitle} ...", end=" ", flush=True)

        async with aiosqlite.connect(DB) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT text FROM texts WHERE id=?", (cid,)) as c:
                row = await c.fetchone()
        if not row:
            progress[book_id]["done"] += 1
            continue

        try:
            url = await tts_chapter(cid, row["text"], voice_id)
            if url is None:
                # Boilerplate/too-short — mark complete with no audio, not an error
                print("(skipped — boilerplate)")
                async with aiosqlite.connect(DB) as db:
                    await db.execute("UPDATE chapters SET status='complete' WHERE id=?", (cid,))
                    await db.execute("UPDATE books SET done=done+1 WHERE id=?", (book_id,))
                    await db.commit()
            else:
                async with aiosqlite.connect(DB) as db:
                    await db.execute("UPDATE chapters SET audio=?, status='complete' WHERE id=?", (url, cid))
                    await db.execute("UPDATE books SET done=done+1 WHERE id=?", (book_id,))
                    await db.commit()
                print("✓")
        except Exception as e:
            errors += 1
            print(f"✗ {e}")
            async with aiosqlite.connect(DB) as db:
                await db.execute("UPDATE chapters SET status='error' WHERE id=?", (cid,))
                await db.commit()

        progress[book_id]["done"] += 1

    final = "error" if errors == len(chapters) else "complete"
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE books SET status=? WHERE id=?", (final, book_id))
        await db.commit()
    progress[book_id]["status"] = final
    print(f"[GEN] Done — {len(chapters)-errors}/{len(chapters)} OK")

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
async def root(): return FileResponse(str(STATIC / "index.html"))

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF only")
    bid = str(uuid.uuid4())
    pdf_path = UPLOADS / f"{bid}.pdf"
    pdf_path.write_bytes(await file.read())
    try:
        doc = fitz.open(str(pdf_path))
    except:
        raise HTTPException(400, "Cannot read PDF")
    # Cover
    page = doc[0]
    zoom = min(600/page.rect.width, 900/page.rect.height, 2.0)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    cover_file = COVERS / f"{bid}.jpg"
    pix.save(str(cover_file))
    # Metadata
    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip()
    author = (meta.get("author") or "").strip()
    if not title:
        lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
        title = lines[0][:80] if lines else Path(file.filename).stem
    # Text + chapters
    full = "\n".join(p.get_text() for p in doc)
    doc.close()
    chapters = split_chapters(full)
    async with aiosqlite.connect(DB) as db:
        await db.execute("INSERT INTO books VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, title, author, f"/covers/{bid}.jpg", len(chapters), 0, "uploaded", "af_bella", time.time()))
        for i, ch in enumerate(chapters):
            cid = str(uuid.uuid4())
            await db.execute("INSERT INTO chapters VALUES (?,?,?,?,?,?,?)",
                (cid, bid, i+1, ch["title"], len(ch["text"].split()), "", "pending"))
            await db.execute("INSERT INTO texts VALUES (?,?)", (cid, ch["text"]))
        await db.commit()
    return {"id": bid, "title": title, "chapters": len(chapters)}

@app.get("/api/books")
async def list_books():
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM books ORDER BY created DESC") as c:
            return [dict(r) for r in await c.fetchall()]

@app.get("/api/books/{bid}")
async def get_book(bid: str):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM books WHERE id=?", (bid,)) as c:
            book = await c.fetchone()
        if not book: raise HTTPException(404)
        async with db.execute(
            "SELECT id,num,title,words,audio,status FROM chapters WHERE book_id=? ORDER BY num", (bid,)) as c:
            chs = await c.fetchall()
    return {**dict(book), "chapters": [dict(c) for c in chs]}

@app.post("/api/books/{bid}/generate")
async def generate(bid: str, background_tasks: BackgroundTasks, voice: str = "af_bella"):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status FROM books WHERE id=?", (bid,)) as c:
            book = await c.fetchone()
        if not book: raise HTTPException(404)
        await db.execute("UPDATE books SET status='generating', voice=?, done=0 WHERE id=?", (voice, bid))
        await db.execute("UPDATE chapters SET status='pending', audio='' WHERE book_id=?", (bid,))
        await db.commit()
    background_tasks.add_task(generate_book, bid, voice)
    return {"ok": True}

@app.get("/api/books/{bid}/progress")
async def get_progress(bid: str):
    if bid in progress:
        return progress[bid]
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status,total,done FROM books WHERE id=?", (bid,)) as c:
            b = await c.fetchone()
    if not b: raise HTTPException(404)
    return {"status": b["status"], "done": b["done"], "total": b["total"]}

@app.delete("/api/books/{bid}")
async def delete_book(bid: str):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT audio FROM chapters WHERE book_id=?", (bid,)) as c:
            for ch in await c.fetchall():
                if ch["audio"]:
                    p = BASE / ch["audio"].lstrip("/")
                    p.unlink(missing_ok=True)
        async with db.execute("SELECT cover FROM books WHERE id=?", (bid,)) as c:
            b = await c.fetchone()
        if b and b["cover"]:
            (BASE / b["cover"].lstrip("/")).unlink(missing_ok=True)
        await db.execute("DELETE FROM texts WHERE id IN (SELECT id FROM chapters WHERE book_id=?)", (bid,))
        await db.execute("DELETE FROM chapters WHERE book_id=?", (bid,))
        await db.execute("DELETE FROM books WHERE id=?", (bid,))
        await db.commit()
    return {"ok": True}

@app.get("/api/voices")
async def list_voices():
    return [{"id": v, "name": n} for v, n in KOKORO_VOICES]

# ── Run ───────────────────────────────────────────────────────────────────────
def find_port(start=7777):
    for p in range(start, start+50):
        with socket.socket() as s:
            try: s.bind(("", p)); return p
            except OSError: continue
    return start

if __name__ == "__main__":
    import uvicorn
    # Railway/cloud hosts set PORT and we should NOT auto-open a browser.
    env_port = os.environ.get("PORT")
    if env_port:
        port = int(env_port)
        print(f"\n  Bookwave is running on port {port}\n")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        port = find_port()
        url = f"http://localhost:{port}"
        print(f"\n  Bookwave is running → {url}\n  Press Ctrl+C to stop.\n")
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
