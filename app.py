import asyncio, os, re, socket, uuid, time, threading, webbrowser, secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode

import aiosqlite
import httpx
try:
    import pymupdf as fitz
except ImportError:
    import fitz

from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request, Depends, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

# ── Paths ─────────────────────────────────────────────────────────────────────
# Storage lives OUTSIDE the project dir so OneDrive doesn't sync audio files.
# Override with env var BOOKWAVE_STORAGE=C:\path\to\storage
BASE   = Path(__file__).parent
STATIC = BASE / "static"
# Storage priority: explicit env -> Railway persistent volume -> Windows LOCALAPPDATA -> repo dir
if os.environ.get("BOOKWAVE_STORAGE"):
    _default_store = Path(os.environ["BOOKWAVE_STORAGE"])
elif Path("/data").exists() and os.access("/data", os.W_OK):
    _default_store = Path("/data/Bookwave")
elif os.environ.get("LOCALAPPDATA"):
    _default_store = Path(os.environ["LOCALAPPDATA"]) / "Bookwave"
else:
    _default_store = BASE / "Bookwave"
STORE  = _default_store
COVERS = STORE / "covers"
AUDIO  = STORE / "audio"
UPLOADS= STORE / "uploads"
DB     = STORE / "books.db"
print(f"[Freedible] storage: {STORE}")

for d in (STATIC, COVERS, AUDIO, UPLOADS):
    d.mkdir(parents=True, exist_ok=True)

# ── Auth config ───────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
SESSION_SECRET       = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
ADMIN_EMAIL          = os.environ.get("ADMIN_EMAIL", "jessheaton001@gmail.com").lower()
AUTH_ENABLED         = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False, max_age=60*60*24*30)
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
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT DEFAULT '',
            picture TEXT DEFAULT '', created REAL)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY, book_id TEXT, reporter_email TEXT,
            reason TEXT, status TEXT DEFAULT 'open', created REAL)""")
        # Migrations for existing deploys
        async with db.execute("PRAGMA table_info(books)") as c:
            cols = {r[1] for r in await c.fetchall()}
        if "user_id" not in cols:
            await db.execute("ALTER TABLE books ADD COLUMN user_id TEXT DEFAULT ''")
        if "visibility" not in cols:
            await db.execute("ALTER TABLE books ADD COLUMN visibility TEXT DEFAULT 'private'")
        if "rights_attestation" not in cols:
            await db.execute("ALTER TABLE books ADD COLUMN rights_attestation INTEGER DEFAULT 0")
        await db.commit()

@app.on_event("startup")
async def startup(): await init_db()

# ── PDF helpers ───────────────────────────────────────────────────────────────
# Numbered chapter: "Chapter 1", "Part II", "Book Three", "Section 2"
CHAP_NUMBERED = re.compile(
    r"^(chapter|part|book|section)\s+"
    r"(\d+|[ivxlcdm]{1,8}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
    r"(?:\s*[:\-–—.]\s*(.{0,80}))?\s*$",
    re.IGNORECASE,
)
# Standalone section: "Prologue", "Introduction", etc.
CHAP_WORD = re.compile(
    r"^(prologue|epilogue|introduction|preface|afterword|foreword|acknowledgments?)"
    r"(?:\s*[:\-–—.]\s*(.{0,80}))?\s*$",
    re.IGNORECASE,
)

def _match_chapter(line):
    s = line.strip()
    if not s or len(s) > 120:
        return None
    m = CHAP_NUMBERED.match(s)
    if m:
        kind, num, rest = m.group(1).title(), m.group(2), m.group(3)
        title = f"{kind} {num}"
        if rest: title += f" — {rest.strip()}"
        return title
    m = CHAP_WORD.match(s)
    if m:
        word, rest = m.group(1).title(), m.group(2)
        return f"{word} — {rest.strip()}" if rest else word
    return None

def split_chapters(text):
    lines, chapters = text.split("\n"), []
    title, buf, found = "Beginning", [], False
    for line in lines:
        t = _match_chapter(line)
        if t:
            body = "\n".join(buf).strip()
            if len(body.split()) >= 40:  # skip tiny fragments
                chapters.append({"title": title, "text": body})
                found = True
            title, buf = t, []
        else:
            buf.append(line)
    body = "\n".join(buf).strip()
    if len(body.split()) >= 40:
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

# ── ePub helpers ──────────────────────────────────────────────────────────────
def _epub_img_ext(data: bytes) -> str:
    if data[:2] == b'\xff\xd8': return '.jpg'
    if data[:4] == b'\x89PNG': return '.png'
    return '.jpg'

def _html_to_tts_text(html_bytes: bytes) -> tuple[str, str]:
    """Strip HTML, extract heading and body text suitable for TTS."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("beautifulsoup4 not installed — run: pip install beautifulsoup4 lxml")

    soup = BeautifulSoup(html_bytes, 'lxml')

    # Drop non-content elements
    for tag in soup.find_all(['script', 'style', 'head', 'nav', 'aside', 'figure', 'figcaption', 'table']):
        tag.decompose()

    # Extract chapter heading before stripping
    heading = ''
    h = soup.find(['h1', 'h2', 'h3'])
    if h:
        heading = h.get_text(' ', strip=True)[:100]
        h.decompose()

    # Insert a period after block-level tags so TTS pauses between paragraphs
    for tag in soup.find_all(['p', 'li', 'blockquote', 'div', 'br', 'h4', 'h5', 'h6']):
        txt = tag.get_text(strip=True)
        if txt and not txt[-1] in '.!?:;':
            tag.append(soup.new_string(' '))

    text = soup.get_text(separator=' ')

    # Remove footnote/endnote markers: [1], (1), superscript unicode digits
    text = re.sub(r'\s*\[\d+\]|\s*\(\d+\)|\s*[¹²³⁴⁵⁶⁷⁸⁹⁰]+', '', text)
    # Remove standalone page labels
    text = re.sub(r'\bPage\s+\d+\b', '', text, flags=re.IGNORECASE)
    # Normalise whitespace
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return heading, text.strip()

def _parse_epub(data: bytes, bid: str) -> dict:
    """Return {title, author, cover_url, chapters:[{title,text}]} from raw ePub bytes."""
    try:
        import ebooklib
        from ebooklib import epub as epublib
    except ImportError:
        raise RuntimeError("ebooklib not installed — run: pip install ebooklib")

    import tempfile
    with tempfile.NamedTemporaryFile(suffix='.epub', delete=False) as f:
        f.write(data)
        tmp = f.name
    try:
        book = epublib.read_epub(tmp, options={'ignore_ncx': True})
    finally:
        Path(tmp).unlink(missing_ok=True)

    # ── Metadata ──────────────────────────────────────────────────────────────
    def _dc(name):
        items = book.get_metadata('DC', name)
        return items[0][0].strip() if items else ''
    title  = _dc('title')
    author = _dc('creator')

    # ── Cover image ───────────────────────────────────────────────────────────
    cover_url = ''
    cover_data = None
    for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
        props = getattr(item, 'properties', []) or []
        name  = (item.get_name() or '').lower()
        iid   = (item.get_id()   or '').lower()
        if 'cover-image' in props or iid in ('cover', 'cover-image', 'coverimage') or ('cover' in name and 'img' not in name):
            cover_data = item.get_content()
            break
    # Fallback: OPF meta reference
    if not cover_data:
        for meta in (book.get_metadata('OPF', 'cover') or []):
            cid = (meta[1] or {}).get('content', '')
            item = book.get_item_with_id(cid) if cid else None
            if item:
                cover_data = item.get_content()
                break
    # Last resort: first image large enough to be a cover
    if not cover_data:
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            d = item.get_content()
            if len(d) > 20_000:
                cover_data = d
                break
    if cover_data:
        ext = _epub_img_ext(cover_data)
        cover_path = COVERS / f"{bid}{ext}"
        cover_path.write_bytes(cover_data)
        cover_url = f"/covers/{bid}{ext}"

    # ── Chapters via spine ────────────────────────────────────────────────────
    spine_ids = [sid for sid, _ in (book.spine or [])]
    doc_map   = {item.get_id(): item for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)}

    chapters: list[dict] = []
    seen: set[str] = set()

    def _add_item(item):
        if item.get_id() in seen:
            return
        seen.add(item.get_id())
        try:
            heading, text = _html_to_tts_text(item.get_content())
            text = clean_text(text)
        except Exception:
            return
        if len(text.split()) < 50:
            return
        ch_title = heading or f"Chapter {len(chapters) + 1}"
        chapters.append({'title': ch_title, 'text': text})

    for sid in spine_ids:
        item = doc_map.get(sid)
        if item:
            _add_item(item)
    # Fallback: any docs not in spine
    if not chapters:
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            _add_item(item)

    # If spine items are too granular (many tiny sections), merge into ~2500-word chunks
    if chapters and all(len(c['text'].split()) < 600 for c in chapters):
        full = '\n\n'.join(c['text'] for c in chapters)
        chapters = split_chapters(full)

    # Final fallback: treat as one text and auto-split
    if not chapters:
        raise ValueError("Could not extract any readable text from this ePub")

    return {'title': title, 'author': author, 'cover_url': cover_url, 'chapters': chapters}

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

def reflow_for_tts(text: str) -> str:
    """PDFs have a hard \\n at every visual line — TTS treats each as a pause.
    Join wrapped lines into running prose so Kokoro only pauses on real punctuation."""
    # Dehyphenate words split across lines: "exam-\nple" -> "example"
    text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)
    # Preserve paragraph breaks as a single sentinel, then collapse single newlines
    text = re.sub(r'\n{2,}', ' \x00 ', text)
    text = text.replace('\n', ' ')
    # If paragraph didn't end on terminal punctuation, add a period so the
    # next sentence has a natural boundary instead of a long silence.
    text = re.sub(r'([^.!?:;"\')\]])\s*\x00\s*', r'\1. ', text)
    text = text.replace('\x00', ' ')
    # Tidy whitespace
    text = re.sub(r'\s{2,}', ' ', text).strip()
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
    text = reflow_for_tts(text)
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
    progress[book_id] = {
        "done": 0, "total": 0, "status": "generating",
        "current": "", "started": time.time(),
    }

    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id,title FROM chapters WHERE book_id=? ORDER BY num", (book_id,)) as c:
            chapters = await c.fetchall()

    progress[book_id]["total"] = len(chapters)
    errors = 0

    for ch in chapters:
        cid, ctitle = ch["id"], ch["title"]
        progress[book_id]["current"] = ctitle
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

# ── Auth helpers ──────────────────────────────────────────────────────────────
async def get_user(request: Request):
    """Returns the current user row from the DB, or None if not signed in."""
    uid = request.session.get("uid")
    if not uid:
        return None
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE id=?", (uid,)) as c:
            row = await c.fetchone()
    return dict(row) if row else None

async def require_user(request: Request):
    u = await get_user(request)
    if not u:
        raise HTTPException(401, "Sign in required")
    return u

async def require_admin(request: Request):
    u = await require_user(request)
    if (u.get("email") or "").lower() != ADMIN_EMAIL:
        raise HTTPException(403, "Admin only")
    return u

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.get("/api/auth/me")
async def auth_me(request: Request):
    u = await get_user(request)
    if not u: return {"user": None}
    return {"user": {
        "id": u["id"], "email": u["email"], "name": u["name"],
        "picture": u["picture"], "is_admin": u["email"].lower() == ADMIN_EMAIL,
    }}

@app.get("/api/auth/google")
async def auth_google(request: Request):
    if not AUTH_ENABLED:
        raise HTTPException(503, "Google sign-in not configured")
    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    redirect_uri = str(request.url_for("auth_callback"))
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}")

@app.get("/api/auth/callback", name="auth_callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    if not AUTH_ENABLED:
        raise HTTPException(503, "Google sign-in not configured")
    if not code or state != request.session.get("oauth_state"):
        raise HTTPException(400, "Invalid OAuth state")
    redirect_uri = str(request.url_for("auth_callback"))
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": redirect_uri, "grant_type": "authorization_code",
        })
        if tok.status_code != 200:
            raise HTTPException(400, f"Token exchange failed: {tok.text}")
        access = tok.json().get("access_token")
        info = await client.get("https://www.googleapis.com/oauth2/v3/userinfo",
                                headers={"Authorization": f"Bearer {access}"})
        if info.status_code != 200:
            raise HTTPException(400, "Userinfo failed")
        data = info.json()
    email = (data.get("email") or "").lower()
    if not email or not data.get("email_verified", True):
        raise HTTPException(400, "No verified email")
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id FROM users WHERE email=?", (email,)) as c:
            existing = await c.fetchone()
        if existing:
            uid = existing["id"]
            await db.execute("UPDATE users SET name=?, picture=? WHERE id=?",
                             (data.get("name",""), data.get("picture",""), uid))
        else:
            uid = str(uuid.uuid4())
            await db.execute("INSERT INTO users VALUES (?,?,?,?,?)",
                             (uid, email, data.get("name",""), data.get("picture",""), time.time()))
        await db.commit()
    request.session["uid"] = uid
    request.session.pop("oauth_state", None)
    return RedirectResponse("/")

@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    request.session.clear()
    return {"ok": True}

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
async def root(): return FileResponse(str(STATIC / "index.html"))

MAX_PDF_BYTES = 40 * 1024 * 1024  # 40 MB
MIN_PDF_BYTES = 2 * 1024           # 2 KB
BLOCKED_TERMS = {
    "xxx", "pornography", "porn ", "explicit sex", "erotica",
    "child abuse", "cp ", "csam", "bestiality", "incest",
}

@app.post("/api/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    user = await require_user(request)
    fname = (file.filename or '').lower()
    is_epub = fname.endswith('.epub')
    is_pdf  = fname.endswith('.pdf')
    if not is_epub and not is_pdf:
        raise HTTPException(400, "PDF or ePub files only")

    data = await file.read()
    if len(data) > MAX_PDF_BYTES:
        raise HTTPException(413, f"File too large (max {MAX_PDF_BYTES // (1024*1024)} MB)")
    if len(data) < MIN_PDF_BYTES:
        raise HTTPException(400, "File too small or empty")

    bid = str(uuid.uuid4())
    upload_path = UPLOADS / f"{bid}{'.epub' if is_epub else '.pdf'}"
    upload_path.write_bytes(data)

    title = author = ''
    cover_url = ''
    chapters: list[dict] = []

    try:
        if is_epub:
            try:
                parsed = _parse_epub(data, bid)
            except Exception as e:
                upload_path.unlink(missing_ok=True)
                raise HTTPException(400, f"Cannot read ePub: {e}")
            title     = parsed['title']
            author    = parsed['author']
            cover_url = parsed['cover_url']
            chapters  = parsed['chapters']
            if not title:
                title = Path(file.filename).stem
        else:
            # ── PDF path ──────────────────────────────────────────────────────
            if data[:5] != b"%PDF-":
                raise HTTPException(400, "Not a valid PDF file")
            try:
                doc = fitz.open(str(upload_path))
            except Exception:
                upload_path.unlink(missing_ok=True)
                raise HTTPException(400, "Cannot read PDF")
            # Cover from first page
            page = doc[0]
            zoom = min(600/page.rect.width, 900/page.rect.height, 2.0)
            pix  = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
            cover_path = COVERS / f"{bid}.jpg"
            pix.save(str(cover_path))
            cover_url = f"/covers/{bid}.jpg"
            # Metadata
            meta   = doc.metadata or {}
            title  = (meta.get("title") or "").strip()
            author = (meta.get("author") or "").strip()
            if not title:
                lines = [l.strip() for l in doc[0].get_text().split("\n") if l.strip()]
                title = lines[0][:80] if lines else Path(file.filename).stem
            full = "\n".join(p.get_text() for p in doc)
            doc.close()
            chapters = split_chapters(full)

        # ── Content moderation (runs for both formats) ────────────────────────
        full_text = ' '.join(c['text'] for c in chapters).lower()
        hits = sum(1 for t in BLOCKED_TERMS if t in full_text)
        if hits >= 2 or any(t in full_text for t in ("csam", "child abuse", "bestiality")):
            upload_path.unlink(missing_ok=True)
            for ext in ('.jpg', '.png'):
                (COVERS / f"{bid}{ext}").unlink(missing_ok=True)
            raise HTTPException(400, "Content not permitted on Freedible")

        async with aiosqlite.connect(DB) as db:
            await db.execute(
                "INSERT INTO books (id,title,author,cover,total,done,status,voice,created,user_id,visibility,rights_attestation) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (bid, title, author, cover_url, len(chapters), 0, "uploaded", "af_bella",
                 time.time(), user["id"], "private", 0))
            for i, ch in enumerate(chapters):
                cid = str(uuid.uuid4())
                await db.execute("INSERT INTO chapters VALUES (?,?,?,?,?,?,?)",
                    (cid, bid, i+1, ch["title"], len(ch["text"].split()), "", "pending"))
                await db.execute("INSERT INTO texts VALUES (?,?)", (cid, ch["text"]))
            await db.commit()
        return {"id": bid, "title": title, "chapters": len(chapters)}

    except HTTPException:
        raise
    except Exception as e:
        upload_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Upload failed: {e}")

def _can_view(book: dict, user: dict | None) -> bool:
    if book.get("visibility") == "public":
        return True
    return bool(user) and book.get("user_id") == user["id"]

def _is_owner(book: dict, user: dict | None) -> bool:
    return bool(user) and book.get("user_id") == user["id"]

@app.get("/api/books")
async def list_books(request: Request, scope: str = "all"):
    """scope=all (default): public + your own. scope=mine: your own only. scope=public: public only."""
    user = await get_user(request)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        if scope == "mine":
            if not user: return []
            q, args = "SELECT * FROM books WHERE user_id=? ORDER BY created DESC", (user["id"],)
        elif scope == "public":
            q, args = "SELECT * FROM books WHERE visibility='public' ORDER BY created DESC", ()
        else:
            if user:
                q, args = ("SELECT * FROM books WHERE visibility='public' OR user_id=? ORDER BY created DESC", (user["id"],))
            else:
                q, args = ("SELECT * FROM books WHERE visibility='public' ORDER BY created DESC", ())
        async with db.execute(q, args) as c:
            rows = [dict(r) for r in await c.fetchall()]
    for r in rows:
        r["is_owner"] = bool(user) and r.get("user_id") == user["id"]
    return rows

@app.get("/api/books/{bid}")
async def get_book(request: Request, bid: str):
    user = await get_user(request)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM books WHERE id=?", (bid,)) as c:
            book = await c.fetchone()
        if not book: raise HTTPException(404)
        book = dict(book)
        if not _can_view(book, user):
            raise HTTPException(404)
        async with db.execute(
            "SELECT id,num,title,words,audio,status FROM chapters WHERE book_id=? ORDER BY num", (bid,)) as c:
            chs = await c.fetchall()
    book["is_owner"] = _is_owner(book, user)
    return {**book, "chapters": [dict(c) for c in chs]}

@app.post("/api/books/{bid}/publish")
async def publish_book(request: Request, bid: str, visibility: str = Form(...), attest: bool = Form(False)):
    user = await require_user(request)
    if visibility not in ("public", "private"):
        raise HTTPException(400, "Invalid visibility")
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id FROM books WHERE id=?", (bid,)) as c:
            row = await c.fetchone()
        if not row: raise HTTPException(404)
        if row["user_id"] != user["id"]:
            raise HTTPException(403, "Not your book")
        if visibility == "public" and not attest:
            raise HTTPException(400, "Must attest ownership or public-domain status to publish")
        await db.execute("UPDATE books SET visibility=?, rights_attestation=? WHERE id=?",
                         (visibility, 1 if attest else 0, bid))
        await db.commit()
    return {"ok": True, "visibility": visibility}

@app.post("/api/books/{bid}/report")
async def report_book(request: Request, bid: str, reason: str = Form("")):
    user = await get_user(request)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT visibility FROM books WHERE id=?", (bid,)) as c:
            row = await c.fetchone()
        if not row or row["visibility"] != "public":
            raise HTTPException(404)
        rid = str(uuid.uuid4())
        await db.execute("INSERT INTO reports VALUES (?,?,?,?,?,?)",
                         (rid, bid, (user or {}).get("email", ""), reason[:500], "open", time.time()))
        await db.commit()
    print(f"[REPORT] Book {bid} reported. Reason: {reason[:200]}")
    return {"ok": True}

@app.get("/api/admin/reports")
async def list_reports(request: Request):
    await require_admin(request)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT r.*, b.title as book_title FROM reports r
            LEFT JOIN books b ON b.id = r.book_id
            ORDER BY r.created DESC""") as c:
            return [dict(r) for r in await c.fetchall()]

@app.post("/api/admin/takedown/{bid}")
async def takedown(request: Request, bid: str):
    await require_admin(request)
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE books SET visibility='private' WHERE id=?", (bid,))
        await db.execute("UPDATE reports SET status='resolved' WHERE book_id=?", (bid,))
        await db.commit()
    return {"ok": True}

@app.post("/api/books/{bid}/generate")
async def generate(request: Request, bid: str, background_tasks: BackgroundTasks, voice: str = "af_bella"):
    user = await require_user(request)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status,user_id FROM books WHERE id=?", (bid,)) as c:
            book = await c.fetchone()
        if not book: raise HTTPException(404)
        if book["user_id"] != user["id"]: raise HTTPException(403, "Not your book")
        await db.execute("UPDATE books SET status='generating', voice=?, done=0 WHERE id=?", (voice, bid))
        await db.execute("UPDATE chapters SET status='pending', audio='' WHERE book_id=?", (bid,))
        await db.commit()
    background_tasks.add_task(generate_book, bid, voice)
    return {"ok": True}

@app.get("/api/books/{bid}/progress")
async def get_progress(request: Request, bid: str):
    user = await get_user(request)
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status,total,done,user_id,visibility FROM books WHERE id=?", (bid,)) as c:
            b = await c.fetchone()
    if not b: raise HTTPException(404)
    if not _can_view(dict(b), user): raise HTTPException(404)
    if bid in progress:
        p = progress[bid]
        eta = None
        if p.get("done", 0) > 0 and p.get("started"):
            elapsed = time.time() - p["started"]
            per_ch = elapsed / p["done"]
            eta = int(per_ch * (p["total"] - p["done"]))
        return {**p, "eta": eta}
    return {"status": b["status"], "done": b["done"], "total": b["total"], "current": "", "eta": None}

@app.delete("/api/books/{bid}")
async def delete_book(request: Request, bid: str):
    user = await require_user(request)
    is_admin = (user.get("email") or "").lower() == ADMIN_EMAIL
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT user_id,cover FROM books WHERE id=?", (bid,)) as c:
            b = await c.fetchone()
        if not b: raise HTTPException(404)
        if b["user_id"] != user["id"] and not is_admin:
            raise HTTPException(403, "Not your book")
        async with db.execute("SELECT audio FROM chapters WHERE book_id=?", (bid,)) as c:
            for ch in await c.fetchall():
                if ch["audio"]:
                    p = BASE / ch["audio"].lstrip("/")
                    p.unlink(missing_ok=True)
        if b["cover"]:
            (BASE / b["cover"].lstrip("/")).unlink(missing_ok=True)
        await db.execute("DELETE FROM texts WHERE id IN (SELECT id FROM chapters WHERE book_id=?)", (bid,))
        await db.execute("DELETE FROM chapters WHERE book_id=?", (bid,))
        await db.execute("DELETE FROM reports WHERE book_id=?", (bid,))
        await db.execute("DELETE FROM books WHERE id=?", (bid,))
        await db.commit()
    return {"ok": True}

@app.get("/api/voices")
async def list_voices():
    return [{"id": v, "name": n} for v, n in KOKORO_VOICES]

SAMPLE_TEXT = "Welcome to Freedible. I'll be your narrator, turning the pages of this book into something you can listen to, anywhere."

@app.get("/api/voices/sample/{voice_id}")
async def voice_sample(voice_id: str):
    valid_ids = {v for v, _ in KOKORO_VOICES}
    if voice_id not in valid_ids:
        raise HTTPException(400, "Unknown voice")
    ext = "mp3" if USE_MODAL else "wav"
    sample_path = AUDIO / f"_sample_{voice_id}.{ext}"
    if not sample_path.exists():
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    _tts_executor, _run_kokoro, SAMPLE_TEXT, voice_id, str(sample_path)
                ), timeout=120
            )
        except Exception as e:
            raise HTTPException(500, f"Sample generation failed: {e}")
    if not sample_path.exists():
        raise HTTPException(500, "Sample not generated")
    return FileResponse(str(sample_path), media_type=f"audio/{ext}")

@app.get("/api/stats")
async def get_stats():
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT COUNT(*) as n FROM books WHERE status='complete' AND visibility='public'") as c:
            books = (await c.fetchone())["n"]
        async with db.execute("""SELECT SUM(c.words) as w FROM chapters c
            JOIN books b ON b.id = c.book_id
            WHERE c.audio != '' AND b.visibility='public'""") as c:
            row = await c.fetchone()
            words = row["w"] or 0
    hours = round(words / 9000)  # ~150 wpm × 60 min
    return {"books": books, "hours": hours}

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
        print(f"\n  Freedible is running on port {port}\n")
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
    else:
        port = find_port()
        url = f"http://localhost:{port}"
        print(f"\n  Freedible is running → {url}\n  Press Ctrl+C to stop.\n")
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
        uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
