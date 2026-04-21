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

# ── Curated public-domain seed library ────────────────────────────────────────
# (gutenberg_id, display_title, author, preferred_voice)
_SEED_CATALOG = [
    (1342,  "Pride and Prejudice",                       "Jane Austen",                "af_bella"),
    (768,   "Wuthering Heights",                         "Emily Brontë",               "bf_emma"),
    (84,    "Frankenstein",                              "Mary Shelley",               "af_nicole"),
    (345,   "Dracula",                                   "Bram Stoker",                "bm_lewis"),
    (174,   "The Picture of Dorian Gray",                "Oscar Wilde",                "bm_george"),
    (1661,  "The Adventures of Sherlock Holmes",         "Arthur Conan Doyle",         "bm_lewis"),
    (2852,  "The Hound of the Baskervilles",             "Arthur Conan Doyle",         "bm_lewis"),
    (1260,  "Jane Eyre",                                 "Charlotte Brontë",           "bf_isabella"),
    (158,   "Emma",                                      "Jane Austen",                "af_bella"),
    (161,   "Sense and Sensibility",                     "Jane Austen",                "af_bella"),
    (105,   "Persuasion",                                "Jane Austen",                "af_bella"),
    (141,   "Mansfield Park",                            "Jane Austen",                "af_bella"),
    (514,   "Little Women",                              "Louisa May Alcott",          "af_bella"),
    (11,    "Alice's Adventures in Wonderland",          "Lewis Carroll",              "af_sarah"),
    (120,   "Treasure Island",                           "Robert Louis Stevenson",     "bm_lewis"),
    (43,    "The Strange Case of Dr Jekyll and Mr Hyde", "Robert Louis Stevenson",     "bm_george"),
    (98,    "A Tale of Two Cities",                      "Charles Dickens",            "bm_george"),
    (1400,  "Great Expectations",                        "Charles Dickens",            "bm_lewis"),
    (730,   "Oliver Twist",                              "Charles Dickens",            "bm_lewis"),
    (74,    "The Adventures of Tom Sawyer",              "Mark Twain",                 "am_adam"),
    (76,    "Adventures of Huckleberry Finn",            "Mark Twain",                 "am_adam"),
    (215,   "The Call of the Wild",                      "Jack London",                "am_michael"),
    (35,    "The Time Machine",                          "H.G. Wells",                 "bm_george"),
    (36,    "The War of the Worlds",                     "H.G. Wells",                 "bm_george"),
    (5200,  "The Metamorphosis",                         "Franz Kafka",                "am_adam"),
    (2701,  "Moby Dick",                                 "Herman Melville",            "am_adam"),
    (2554,  "Crime and Punishment",                      "Fyodor Dostoevsky",          "am_michael"),
    (2148,  "Anna Karenina",                             "Leo Tolstoy",                "af_nicole"),
    (2600,  "War and Peace",                             "Leo Tolstoy",                "bm_george"),
    (28054, "The Brothers Karamazov",                    "Fyodor Dostoevsky",          "am_michael"),
    (2680,  "Meditations",                               "Marcus Aurelius",            "bm_george"),
    (132,   "The Art of War",                            "Sun Tzu",                    "bm_george"),
    (1232,  "The Prince",                                "Niccolò Machiavelli",        "bm_george"),
    (5740,  "The Republic",                              "Plato",                      "bm_george"),
    (8800,  "Thus Spoke Zarathustra",                    "Friedrich Nietzsche",        "bm_george"),
    (1184,  "The Count of Monte Cristo",                 "Alexandre Dumas",            "bm_george"),
    (164,   "Twenty Thousand Leagues Under the Sea",     "Jules Verne",                "bm_george"),
    (4085,  "Around the World in Eighty Days",           "Jules Verne",                "bm_george"),
    (996,   "Don Quixote",                               "Miguel de Cervantes",        "bm_george"),
    (135,   "Les Misérables",                            "Victor Hugo",                "bm_george"),
    (25344, "The Scarlet Letter",                        "Nathaniel Hawthorne",        "af_nicole"),
    (55,    "The Wonderful Wizard of Oz",                "L. Frank Baum",              "af_sarah"),
    (35997, "The Jungle Book",                           "Rudyard Kipling",            "bm_lewis"),
    (1727,  "The Odyssey",                               "Homer",                      "bm_george"),
    (64317, "The Great Gatsby",                          "F. Scott Fitzgerald",        "am_adam"),
    (203,   "Uncle Tom's Cabin",                         "Harriet Beecher Stowe",      "af_nicole"),
    (16,    "Peter Pan",                                 "J.M. Barrie",               "af_sarah"),
    (1080,  "A Modest Proposal",                         "Jonathan Swift",             "bm_lewis"),
]

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

# ── Static pages (legal, blog) ────────────────────────────────────────────────
from fastapi.responses import HTMLResponse

_SITE_CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet"/>
<link rel="stylesheet" href="/static/style.css?v=14"/>
<script defer data-domain="freedible.co.uk" src="https://plausible.io/js/script.js"></script>
<style>
.prose{max-width:720px;margin:0 auto;padding:48px 24px 100px}
.prose h1{font-size:38px;font-weight:800;letter-spacing:-.8px;margin-bottom:14px;line-height:1.12}
.prose h2{font-size:22px;font-weight:700;margin:40px 0 12px;letter-spacing:-.3px}
.prose h3{font-size:17px;font-weight:700;margin:26px 0 8px}
.prose p{color:#c8c8e0;line-height:1.78;margin-bottom:16px;font-size:16px}
.prose ul,.prose ol{color:#c8c8e0;padding-left:24px;margin-bottom:16px;line-height:1.78}
.prose li{margin-bottom:6px}
.prose a{color:#f17b2a;text-decoration:none}
.prose a:hover{text-decoration:underline}
.prose .lead{font-size:19px;color:#d8d8f0;margin-bottom:32px;line-height:1.65;font-weight:400}
.prose .callout{background:#17172b;border:1px solid #2a2a45;border-left:4px solid #f17b2a;border-radius:0 10px 10px 0;padding:18px 22px;margin:28px 0}
.prose .callout p{margin:0;color:#d8d8f0;font-size:15px}
.prose hr{border:none;border-top:1px solid #2a2a45;margin:40px 0}
.prose .tag{display:inline-block;background:#1d1d30;color:#f17b2a;border:1px solid rgba(241,123,42,.3);border-radius:6px;padding:3px 10px;font-size:11px;font-weight:700;margin-bottom:20px;letter-spacing:.6px;text-transform:uppercase}
.bc{font-size:13px;color:#8888aa;margin-bottom:36px;display:flex;align-items:center;gap:6px}
.bc a{color:#8888aa;text-decoration:none}.bc a:hover{color:#f0f0fa}
.ph{background:#0e0e18;border-bottom:1px solid #2a2a45;padding:0 24px;height:62px;display:flex;align-items:center}
.ph-logo{font-weight:800;font-size:20px;color:#f0f0fa;text-decoration:none;letter-spacing:-.5px}
.ph-logo em{color:#f17b2a;font-style:normal}
.pf{text-align:center;padding:36px 24px;border-top:1px solid #2a2a45;color:#8888aa;font-size:13px;margin-top:0}
.pf a{color:#8888aa;text-decoration:none;margin:0 10px}.pf a:hover{color:#f0f0fa}
.pf-links{display:flex;justify-content:center;flex-wrap:wrap;gap:4px;margin-bottom:12px}
.blog-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:20px;margin-top:32px}
.blog-card{background:#17172b;border:1px solid #2a2a45;border-radius:14px;padding:24px;text-decoration:none;display:block;transition:border-color .15s,transform .15s}
.blog-card:hover{border-color:#f17b2a44;transform:translateY(-2px)}
.blog-card-tag{font-size:10px;font-weight:700;letter-spacing:.8px;text-transform:uppercase;color:#f17b2a;margin-bottom:8px}
.blog-card-title{font-size:17px;font-weight:700;color:#f0f0fa;margin-bottom:8px;line-height:1.35}
.blog-card-desc{font-size:14px;color:#8888aa;line-height:1.6}
.prose-date{font-size:12px;color:#8888aa;margin-bottom:24px;display:block}
</style>"""

def _ph():
    return '<div class="ph"><a class="ph-logo" href="/">Free<em>dible</em></a></div>'

def _pf():
    return '''<footer class="pf"><div class="pf-links">
<a href="/">Home</a><a href="/terms">Terms</a><a href="/privacy">Privacy</a>
<a href="/dmca">DMCA</a><a href="/accessibility">Accessibility</a><a href="/blog">Blog</a>
</div><div>© 2025 Freedible · Made in the UK · <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a></div></footer>'''

def _page(title: str, desc: str, body: str, canonical: str = "") -> HTMLResponse:
    can = f'<link rel="canonical" href="https://www.freedible.co.uk{canonical}"/>' if canonical else ""
    html = f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title><meta name="description" content="{desc}"/>
<meta property="og:title" content="{title}"/><meta property="og:description" content="{desc}"/>
{can}{_SITE_CSS}</head><body>
{_ph()}<div class="prose">{body}</div>{_pf()}</body></html>"""
    return HTMLResponse(html)

# ── Legal pages ────────────────────────────────────────────────────────────────
@app.get("/terms")
async def page_terms():
    return _page("Terms of Service — Freedible",
                 "Freedible terms of service. User responsibilities, content licensing, and platform rules.",
                 canonical="/terms", body="""
<div class="tag">Legal</div>
<h1>Terms of Service</h1>
<p class="prose-date">Last updated: April 2025</p>
<p class="lead">By using Freedible you agree to these terms. Please read them.</p>

<h2>1. What Freedible does</h2>
<p>Freedible is a platform that uses AI text-to-speech technology to narrate books. We provide tools to browse community-shared public domain audiobooks and to privately convert files you upload.</p>

<h2>2. Your uploads — your responsibility</h2>
<p>When you upload a file to Freedible, you warrant that:</p>
<ul>
<li>You are the copyright owner of the uploaded work, <strong>or</strong></li>
<li>The work is in the public domain in your jurisdiction, <strong>or</strong></li>
<li>You have a lawful basis to process it under applicable copyright law (including format-shifting for accessibility under CDPA 1988 s.31A/B or equivalent)</li>
</ul>
<p>Private uploads are processed solely for your personal listening. You must not upload content belonging to third parties for the purpose of making it publicly available on Freedible without their authorisation.</p>

<h2>3. Licence you grant us</h2>
<p>By uploading content, you grant Freedible a limited, non-exclusive, revocable licence to process and temporarily store that content solely for the purpose of generating your audiobook. We do not claim ownership of your content. Private uploads are never shared without your explicit action.</p>

<h2>4. Community sharing</h2>
<p>When you choose to publish a book to the Community Library, you confirm by attestation that you have the rights to do so. You grant other Freedible users a non-commercial licence to listen. You may retract public availability at any time.</p>

<h2>5. Prohibited content</h2>
<p>You may not upload or publish: sexually explicit material, content depicting minors in any harmful context, material that incites violence or hatred, malware or harmful code, or any content that infringes third-party rights.</p>

<h2>6. Indemnification</h2>
<p>You agree to indemnify and hold harmless Freedible, its operators, and affiliates from any claim, damage, or expense (including reasonable legal fees) arising from your use of the platform, your uploads, or your violation of these terms.</p>

<h2>7. Disclaimer of warranties</h2>
<p>Freedible is provided "as is" without warranty of any kind. We do not guarantee uptime, audio quality, or availability of any specific feature. AI narration may contain errors.</p>

<h2>8. Limitation of liability</h2>
<p>To the fullest extent permitted by law, Freedible shall not be liable for indirect, incidental, or consequential damages arising from your use of the service.</p>

<h2>9. Governing law</h2>
<p>These terms are governed by the laws of England and Wales. Any disputes shall be subject to the exclusive jurisdiction of the courts of England and Wales.</p>

<h2>10. Changes</h2>
<p>We may update these terms. Continued use after changes constitutes acceptance. We'll note the date at the top of this page.</p>

<p>Questions? <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a></p>""")

@app.get("/privacy")
async def page_privacy():
    return _page("Privacy Policy — Freedible",
                 "How Freedible collects, stores, and uses your data. GDPR compliant, UK-based.",
                 canonical="/privacy", body="""
<div class="tag">Legal</div>
<h1>Privacy Policy</h1>
<p class="prose-date">Last updated: April 2025</p>
<p class="lead">We're UK-based and take your privacy seriously. Here's exactly what we collect and why.</p>

<h2>Who we are</h2>
<p>Freedible is operated from the United Kingdom. For GDPR purposes we are the data controller. Contact: <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a></p>

<h2>What we collect</h2>
<h3>When you sign in with Google</h3>
<ul>
<li>Your name, email address, and profile picture — provided by Google OAuth</li>
<li>We do not receive your Google password</li>
</ul>
<h3>When you upload a file</h3>
<ul>
<li>The file itself, stored securely to generate your audiobook</li>
<li>The generated audio files and book metadata (title, author)</li>
<li>Private uploads are only accessible by you</li>
</ul>
<h3>Usage data (analytics)</h3>
<p>We use Plausible Analytics — a privacy-first, GDPR-compliant analytics tool. Plausible does not use cookies, does not track individuals across sites, and does not collect personal data. We see aggregate page-view counts only.</p>

<h2>How we use your data</h2>
<ul>
<li><strong>To provide the service</strong> — storing your books, generating audio, saving your listening position</li>
<li><strong>To authenticate you</strong> — we use your email to identify your account</li>
<li><strong>To contact you</strong> — only if you reach out to us, or in the event of a serious security or legal issue</li>
</ul>
<p>We do not sell your data. We do not use your data for advertising. We do not share your data with third parties except as required by law.</p>

<h2>Data storage</h2>
<p>Your data is stored on Railway (railway.app), a cloud hosting platform. Servers are located in the EU (Google Cloud). Book files and audio are stored on persistent volumes. We take reasonable technical measures to secure your data.</p>

<h2>Your rights (GDPR)</h2>
<p>As a UK/EU resident you have the right to: access your data, rectify inaccurate data, erase your data, restrict processing, and data portability. To exercise any right, email <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a> and we'll respond within 30 days.</p>
<p>You can delete your account and all associated data by emailing us. Book deletion is also available directly in the app.</p>

<h2>Cookies</h2>
<p>We use a single session cookie to keep you signed in. It contains only your session ID — no personal data. No advertising cookies. No third-party tracking cookies.</p>

<h2>Data retention</h2>
<p>We retain your account data for as long as your account exists. Deleted books are removed from our servers within 30 days. Analytics data (aggregated, non-personal) may be retained indefinitely.</p>

<h2>Children</h2>
<p>Freedible is not directed at children under 13. We do not knowingly collect data from children.</p>

<h2>Changes</h2>
<p>We'll update the date at the top if this policy changes materially. Questions: <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a></p>""")

@app.get("/dmca")
async def page_dmca():
    return _page("DMCA & Copyright Policy — Freedible",
                 "How to submit a copyright takedown request to Freedible. We respond within 24 hours.",
                 canonical="/dmca", body="""
<div class="tag">Legal</div>
<h1>Copyright &amp; DMCA Policy</h1>
<p class="prose-date">Last updated: April 2025</p>
<p class="lead">Freedible respects intellectual property rights. We have a clear, responsive process for copyright concerns — this is our actual legal shield and we take it seriously.</p>

<div class="callout"><p><strong>To report infringing content:</strong> email <a href="mailto:dmca@freedible.co.uk">dmca@freedible.co.uk</a> with the information below. We commit to a 24-hour acknowledgement and action within 72 hours on valid notices.</p></div>

<h2>Our platform model</h2>
<p>Freedible operates as a platform (host), not a publisher. Users upload content; we process it to generate audio. Private uploads are only accessible to the uploader. Public content has been attested by the uploader as either owned by them or in the public domain.</p>
<p>We rely on the safe harbour provisions of the Electronic Commerce (EC Directive) Regulations 2002 (UK) and cooperate fully with the DMCA (US) notice-and-takedown framework.</p>

<h2>How to submit a takedown notice</h2>
<p>Email <a href="mailto:dmca@freedible.co.uk">dmca@freedible.co.uk</a> with the following:</p>
<ol>
<li><strong>Identification of the copyrighted work</strong> — the title, author, and original publication details</li>
<li><strong>Identification of the infringing material</strong> — the URL on Freedible where the material appears</li>
<li><strong>Your contact information</strong> — name, email, postal address, phone number</li>
<li><strong>A statement of good faith</strong> — "I have a good faith belief that use of the material in the manner complained of is not authorised by the copyright owner, its agent, or the law"</li>
<li><strong>A statement of accuracy</strong> — "I swear, under penalty of perjury, that the information in the notification is accurate, and that I am the copyright owner or am authorised to act on behalf of the copyright owner"</li>
<li><strong>Your signature</strong> — typed name is sufficient for email notices</li>
</ol>

<h2>What happens next</h2>
<ul>
<li><strong>Within 24 hours:</strong> We acknowledge your notice by email</li>
<li><strong>Within 72 hours:</strong> We investigate and, if the notice is valid, remove or disable access to the content</li>
<li><strong>We notify the uploader</strong> that their content has been removed and provide them the opportunity to submit a counter-notice</li>
</ul>

<h2>Counter-notices</h2>
<p>If you believe content was removed in error, you may submit a counter-notice to <a href="mailto:dmca@freedible.co.uk">dmca@freedible.co.uk</a> including: identification of the removed content, a statement that you consent to the jurisdiction of the courts, and a statement under penalty of perjury that the content was removed by mistake or misidentification.</p>

<h2>Repeat infringers</h2>
<p>Freedible will terminate the accounts of users who are determined to be repeat infringers.</p>

<h2>Public domain &amp; accessibility</h2>
<p>Works in the public domain cannot be the subject of a valid copyright claim. Under UK law (CDPA 1988 s.31A/B), format-shifting for accessibility purposes by qualifying persons is also lawful. If you believe a takedown notice was filed in bad faith, please let us know.</p>

<p>All copyright questions: <a href="mailto:dmca@freedible.co.uk">dmca@freedible.co.uk</a></p>""")

@app.get("/accessibility")
async def page_accessibility():
    return _page("Accessibility — Freedible",
                 "Freedible is built for people with dyslexia, visual impairment, reading fatigue, and ADHD. Free, legal, and protected under UK law.",
                 canonical="/accessibility", body="""
<div class="tag">Accessibility</div>
<h1>Freedible is built for accessibility</h1>
<p class="lead">If books have ever felt out of reach — because of dyslexia, vision, fatigue, or attention — this is for you. Freedible turns any book into a natural audiobook, free, with no hoops to jump through.</p>

<h2>Who we're built for</h2>
<p>We designed Freedible with these listeners at the centre:</p>
<ul>
<li><strong>Dyslexia</strong> — estimated 10% of the UK population. Audiobooks remove the visual decoding barrier entirely, letting you focus on the ideas.</li>
<li><strong>Visual impairment</strong> — whether you're partially sighted or fully blind, an audiobook is often the most practical way to access a text. Freedible works with screen readers and keyboard navigation.</li>
<li><strong>Reading fatigue</strong> — chronic illness, long-COVID, migraines, and eye strain make sustained reading painful. Listening is a direct alternative.</li>
<li><strong>ADHD</strong> — many people with ADHD find audio easier to focus on than text, especially with speed control (try 1.25× or 1.5×).</li>
<li><strong>Long commutes &amp; busy lives</strong> — not a disability, but a very real reason to want books in audio form.</li>
</ul>

<h2>What UK law says</h2>
<p>The Copyright, Designs and Patents Act 1988 (CDPA), sections 31A and 31B, provide an explicit exception for format-shifting copyrighted works into accessible formats for people with a "print disability." The UK has also ratified the Marrakesh Treaty, which extends these rights internationally.</p>
<p>If you have dyslexia, a visual impairment, or another condition that makes reading standard text difficult, converting a book you legally own into an audiobook is <strong>protected under UK law</strong>. This is not a legal grey area — it is an explicit statutory exception.</p>

<div class="callout"><p><strong>In plain English:</strong> if you have a print disability and you own the book, you are legally entitled to convert it to audio for your own use. Freedible is a tool to do exactly that.</p></div>

<h2>How Freedible helps</h2>
<ul>
<li><strong>Natural AI voices</strong> — not the robotic monotone of older TTS. Modern voices have natural cadence, pauses, and expression.</li>
<li><strong>Speed control</strong> — listen at 0.75× to 2.0×. Many people with dyslexia actually prefer slightly faster audio.</li>
<li><strong>Volume boost</strong> — go beyond 100% if you need it.</li>
<li><strong>Bookmarks</strong> — your position is saved automatically every 5 seconds.</li>
<li><strong>Chapter navigation</strong> — skip to any chapter, or use the chapter panel during playback.</li>
<li><strong>No app</strong> — works in any browser on any device.</li>
</ul>

<h2>Screen reader &amp; keyboard support</h2>
<p>Freedible is built on semantic HTML. The player responds to standard media keyboard shortcuts. If you find any accessibility barrier, please email <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a> — we will fix it.</p>

<h2>Cost</h2>
<p>Freedible is free. It will remain free. Accessibility should not be a premium feature.</p>

<h2>Recommended reads</h2>
<p>If you're new to audiobooks and not sure where to start, our <a href="/blog/audiobooks-dyslexia-visual-impairment">guide to audiobooks for dyslexia and visual impairment</a> has practical recommendations.</p>

<p>Questions or access needs: <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a></p>""")

# ── Blog ───────────────────────────────────────────────────────────────────────
_BLOG_POSTS = {
    "best-free-public-domain-audiobooks": {
        "title": "The 12 Best Free Public Domain Audiobooks to Listen to in 2025",
        "tag": "Public Domain",
        "desc": "Discover the best free public domain audiobooks you can listen to right now — from Jane Austen to Marcus Aurelius, all narrated with natural AI voices on Freedible.",
        "date": "April 2025",
        "body": """
<div class="tag">Public Domain</div>
<h1>The 12 Best Free Public Domain Audiobooks to Listen to in 2025</h1>
<span class="prose-date">April 2025 · 8 min read</span>
<p class="lead">Public domain books are works whose copyright has expired — they belong to everyone, forever. That means you can read, share, adapt, and listen to them legally, completely free. Here are twelve of the best.</p>

<h2>What is the public domain?</h2>
<p>In the UK, copyright lasts for the author's lifetime plus 70 years. In the US, works published before 1928 are in the public domain. This means the greatest literature of the 19th century and much of the early 20th is freely available — including some of the most beloved books ever written.</p>

<h2>The list</h2>

<h3>1. Pride and Prejudice — Jane Austen (1813)</h3>
<p>Perhaps the most re-read novel in the English language, and for good reason. Austen's wit is as sharp in audio as on the page — arguably sharper, because you hear the irony. Perfect for commutes.</p>

<h3>2. Wuthering Heights — Emily Brontë (1847)</h3>
<p>Darker and stranger than most people expect. Brontë's gothic moorland novel has a fractured, frame-within-frame structure that rewards listening. The prose is dense in a way that audio actually makes easier.</p>

<h3>3. Meditations — Marcus Aurelius (c. 170 AD)</h3>
<p>The private journal of a Roman emperor. Written in Greek, never intended for publication. One of the most practical philosophy books ever written — and short enough to finish on a long train journey.</p>

<h3>4. The Art of War — Sun Tzu (c. 500 BC)</h3>
<p>Thirteen short chapters on strategy that have remained relevant for 2,500 years. At under two hours as an audiobook, there's no excuse not to.</p>

<h3>5. The Adventures of Sherlock Holmes — Arthur Conan Doyle (1892)</h3>
<p>Short stories are ideal for audio — each one is self-contained, typically under 30 minutes. Doyle's plotting is as tight now as it was in 1892.</p>

<h3>6. Dracula — Bram Stoker (1897)</h3>
<p>Told entirely through journal entries, letters, and newspaper clippings — a format that translates beautifully to audio narration. Genuinely unsettling, even now.</p>

<h3>7. The Picture of Dorian Gray — Oscar Wilde (1890)</h3>
<p>Wilde's only novel. Witty, dark, and full of epigrams that land differently when heard aloud. The dialogue, in particular, is extraordinary.</p>

<h3>8. Crime and Punishment — Fyodor Dostoevsky (1866)</h3>
<p>The definitive psychological thriller, written 150 years before the genre existed. Long, but the tension never drops. Ideal for long commutes.</p>

<h3>9. Moby Dick — Herman Melville (1851)</h3>
<p>Famously difficult to read on the page; surprisingly compelling as audio. The cetological chapters (on whale anatomy) that put so many readers off become meditative rather than boring when listened to.</p>

<h3>10. Nineteen Eighty-Four — George Orwell (1949)</h3>
<p>Now in the public domain in the UK (Orwell died in 1950; 70 years expired in 2020). The most important political novel of the 20th century, and unfortunately more relevant than ever.</p>

<h3>11. Don Quixote — Miguel de Cervantes (1605)</h3>
<p>Often called the first modern novel. Funnier than you'd expect, and the first work of fiction to seriously interrogate the nature of fiction itself. The audiobook runs about 40 hours — plan accordingly.</p>

<h3>12. The Great Gatsby — F. Scott Fitzgerald (1925)</h3>
<p>Now public domain in both the US and UK. Short (five hours as an audiobook), precise, and devastating. One of the few novels where almost every sentence rewards rereading — or re-listening.</p>

<h2>How to listen for free</h2>
<p>All of these books are available on Freedible. You can upload a public domain ePub or PDF and generate a natural-sounding audiobook in minutes — choosing from multiple AI voices. It's completely free.</p>
<p><a href="/">Start listening on Freedible →</a></p>""",
    },
    "convert-epub-pdf-to-audiobook-free": {
        "title": "How to Convert an ePub or PDF to Audiobook Free (2025 Guide)",
        "tag": "How-To",
        "desc": "Step-by-step guide to converting any ePub or PDF into a natural-sounding audiobook for free using Freedible's AI narration.",
        "date": "April 2025",
        "body": """
<div class="tag">How-To</div>
<h1>How to Convert an ePub or PDF to Audiobook Free (2025 Guide)</h1>
<span class="prose-date">April 2025 · 5 min read</span>
<p class="lead">You have a book. You want to listen to it. Here's exactly how to turn any ePub or PDF into a high-quality audiobook in under five minutes — for free.</p>

<h2>What you need</h2>
<ul>
<li>A free Freedible account (sign in with Google — takes 10 seconds)</li>
<li>The book as an ePub or PDF file</li>
<li>That's it</li>
</ul>

<h2>Step 1: Get your book file</h2>
<p>If you've bought an ebook from a retailer like Kobo or directly from a publisher, you likely already have an ePub or PDF. Most ebook platforms let you download DRM-free versions of your purchases, especially for public domain titles.</p>
<p>For public domain books, <a href="https://www.gutenberg.org" target="_blank" rel="noopener">Project Gutenberg</a> and <a href="https://standardebooks.org" target="_blank" rel="noopener">Standard Ebooks</a> are the best sources — Standard Ebooks in particular produces beautifully typeset ePub files that convert extremely well.</p>

<div class="callout"><p><strong>Legal note:</strong> Only convert books you own, or that are in the public domain. If you have a print disability, UK law (CDPA 1988 s.31A/B) also permits format-shifting books you've legally acquired. See our <a href="/accessibility">accessibility page</a> for details.</p></div>

<h2>Step 2: Sign in to Freedible</h2>
<p>Go to <a href="/">freedible.co.uk</a> and click "Get started free." Sign in with your Google account — no new password to remember.</p>

<h2>Step 3: Upload your file</h2>
<p>Click "Upload a Book" (or "Add a Book" in the header). Drag and drop your ePub or PDF into the upload area, or click to browse. Files up to 40MB are supported. The upload takes a few seconds.</p>
<p>Freedible reads the book structure automatically — chapters, title, author. For ePubs, it uses the book's own chapter structure. For PDFs, it detects chapter headings in the text.</p>

<h2>Step 4: Choose a voice</h2>
<p>You'll see a voice selector with multiple options. Hit "Preview" to hear a 5-second sample of each voice before committing. Different voices suit different books — a warmer voice for fiction, a crisper one for non-fiction.</p>

<h2>Step 5: Generate and listen</h2>
<p>Click "Generate Audiobook." The first chapter will be ready to play within about a minute. Freedible generates chapter by chapter, so you can start listening immediately while the rest generates in the background.</p>
<p>Your progress is saved automatically — come back any time and it remembers where you were.</p>

<h2>Tips for the best results</h2>
<ul>
<li><strong>ePub beats PDF</strong> — ePubs have structured chapter information; PDFs don't always, especially scans</li>
<li><strong>Standard Ebooks are the best source</strong> — their ePubs are clean, well-structured, and generate perfectly</li>
<li><strong>Try 1.25× speed</strong> — most people find a slight speed boost makes listening more engaging</li>
<li><strong>Use bookmarks</strong> — your place is saved automatically, but you can also bookmark manually</li>
</ul>

<h2>What about sharing?</h2>
<p>By default, your book is private — only you can see and hear it. If you want to share a public domain book with the community, you can publish it to the Community Library. You'll be asked to confirm you have the rights to do so.</p>

<p><a href="/">Try it now at Freedible →</a></p>""",
    },
    "audiobooks-dyslexia-visual-impairment": {
        "title": "Audiobooks for Dyslexia and Visual Impairment: A Complete UK Guide",
        "tag": "Accessibility",
        "desc": "How audiobooks help with dyslexia, visual impairment, ADHD, and reading fatigue. Includes UK legal protections for format-shifting under the CDPA 1988.",
        "date": "April 2025",
        "body": """
<div class="tag">Accessibility</div>
<h1>Audiobooks for Dyslexia and Visual Impairment: A Complete UK Guide</h1>
<span class="prose-date">April 2025 · 7 min read</span>
<p class="lead">Around 10% of the UK population has dyslexia. Millions more have visual impairments, ADHD, chronic illness, or reading fatigue. Audiobooks are often the best — and sometimes the only — way to access a text. Here's what you need to know.</p>

<h2>How audiobooks help with dyslexia</h2>
<p>Dyslexia is a difference in how the brain processes written language — it's not a problem with intelligence or comprehension. Audiobooks remove the visual decoding step entirely, allowing full access to complex ideas and narratives without the friction of decoding text.</p>
<p>Research consistently shows that listening comprehension in dyslexic individuals is often equal to or exceeds that of non-dyslexic readers. The barrier is the text, not the understanding.</p>
<p>Speed control is particularly useful: many dyslexic listeners prefer slightly faster audio (1.25× to 1.5×), which keeps the brain engaged without losing comprehension. Freedible lets you set any speed from 0.75× to 2×.</p>

<h2>Visual impairment and print disability</h2>
<p>The term "print disability" in UK law covers: blindness, partial sight, and any condition that prevents a person from reading standard printed text — including dyslexia, physical disabilities that prevent holding a book, and some cognitive conditions.</p>
<p>If you have a print disability, audiobooks aren't just useful — they're often the primary means of accessing literature and information.</p>

<h2>ADHD and reading fatigue</h2>
<p>Many people with ADHD find sustained silent reading difficult, particularly for long books, even when they're highly intelligent and motivated. Audio engages a different attention mechanism. Many ADHD readers find they can listen to a 400-page book that would take months to read on paper.</p>
<p>Reading fatigue — from chronic illness, long-COVID, migraines, post-surgery recovery, or simply a demanding screen-heavy job — is a real barrier that audiobooks address directly.</p>

<h2>What UK law says</h2>
<p>The Copyright, Designs and Patents Act 1988, sections 31A and 31B (as amended by the Enterprise and Regulatory Reform Act 2013 and SI 2014/1384), provide explicit exceptions allowing people with a "print disability" to make accessible copies of works they have lawfully acquired.</p>
<p>Specifically:</p>
<ul>
<li>A person with a print disability may make an accessible format copy of a work for their personal use</li>
<li>Designated bodies (charities, libraries) may make and distribute accessible copies</li>
<li>These exceptions apply even when the work is still in copyright</li>
</ul>
<p>The UK has also ratified the Marrakesh Treaty to Facilitate Access to Published Works for Persons Who Are Blind, Visually Impaired, or Otherwise Print Disabled — an international framework that reinforces these rights.</p>

<div class="callout"><p><strong>In plain English:</strong> If you have dyslexia, a visual impairment, or another print disability — and you've legally acquired the book — converting it to an audiobook for your personal use is protected under UK law. Freedible is a tool to do exactly that.</p></div>

<h2>Practical recommendations</h2>
<h3>For fiction</h3>
<p>Start with something you've always wanted to read but felt put off by length. Wuthering Heights, Pride and Prejudice, and Crime and Punishment are all freely available as public domain audiobooks on Freedible. The Sherlock Holmes short stories are ideal if you're new to audio — each story is under 30 minutes.</p>
<h3>For non-fiction</h3>
<p>Meditations by Marcus Aurelius is short, profound, and reads beautifully aloud. The Art of War is under two hours. Both are freely available.</p>
<h3>For your own books</h3>
<p>Upload any ePub or PDF you legally own to Freedible — it converts privately, for your ears only. Standard Ebooks (standardebooks.org) produces the cleanest public domain ePubs available.</p>

<h2>Tools that help</h2>
<p>Freedible includes several features designed with accessibility in mind:</p>
<ul>
<li><strong>Speed control</strong> — tap any speed from 0.75× to 2×</li>
<li><strong>Volume boost</strong> — beyond the browser's normal 100% limit</li>
<li><strong>Automatic bookmarks</strong> — saved every 5 seconds</li>
<li><strong>OS media controls</strong> — your phone's lock screen controls work</li>
<li><strong>No app required</strong> — works in any browser</li>
</ul>

<p>Questions about accessibility: <a href="mailto:hello@freedible.co.uk">hello@freedible.co.uk</a></p>
<p><a href="/accessibility">See our full accessibility page →</a> · <a href="/">Start listening on Freedible →</a></p>""",
    },
    "ai-audiobook-narration-2025": {
        "title": "AI Audiobook Narration in 2025: Is It Actually Good Enough?",
        "tag": "AI Narration",
        "desc": "An honest look at how AI audiobook narration compares to human narrators in 2025. Where it works, where it falls short, and what's changed.",
        "date": "April 2025",
        "body": """
<div class="tag">AI Narration</div>
<h1>AI Audiobook Narration in 2025: Is It Actually Good Enough?</h1>
<span class="prose-date">April 2025 · 6 min read</span>
<p class="lead">The honest answer: it depends on the book. But the gap between AI and professional human narration has closed dramatically in the last two years. Here's an honest assessment.</p>

<h2>What changed</h2>
<p>For most of the 2010s, text-to-speech was obviously robotic. Flat intonation, mispronounced names, and no sense of rhythm made it useful for navigation instructions but uncomfortable for 8 hours of Tolstoy.</p>
<p>The shift started around 2022 with neural TTS systems, and accelerated sharply in 2023–2024. Models like Kokoro (which Freedible uses) are trained on diverse speech data and produce natural prosody — the rhythm and emphasis that makes speech feel human.</p>

<h2>Where AI narration is genuinely good</h2>
<p><strong>Non-fiction and essays.</strong> Philosophy, history, self-help, science writing — content where the ideas matter more than dramatic performance. Meditations by Marcus Aurelius sounds excellent in AI narration. So does most popular non-fiction.</p>
<p><strong>Classic literature.</strong> Austen, Dickens, Dostoevsky — the prose is dense and the emotional register is relatively consistent. AI handles this well.</p>
<p><strong>Short stories.</strong> The Sherlock Holmes stories, Chekhov, Poe — self-contained, plot-driven, relatively even in tone. AI narration works very well here.</p>
<p><strong>Any book you otherwise wouldn't listen to at all.</strong> If the choice is AI narration or not hearing the book, AI narration wins every time.</p>

<h2>Where human narration still wins</h2>
<p><strong>Character-heavy dialogue.</strong> A skilled human narrator gives each character a distinct voice. Current AI voices are consistent but uniform — everyone sounds like the same narrator.</p>
<p><strong>Poetry.</strong> Metre, rhythm, breath, and silence are everything in poetry. AI can recite; it can't yet perform.</p>
<p><strong>Humour.</strong> Comic timing requires genuine understanding of what's funny. AI gets cadence wrong in ways that flatten jokes.</p>
<p><strong>Very long books.</strong> For a 40-hour audiobook, voice variation and performance quality matter more. A good human narrator carries you through; AI can become monotonous.</p>

<h2>What Freedible uses</h2>
<p>Freedible uses Kokoro, an open-source neural TTS model that runs on GPU. It offers multiple voice options — different characters, accents, and tones — and produces natural sentence rhythm and paragraph pacing.</p>
<p>The voice preview feature lets you hear 5 seconds of each voice before committing, so you can choose what suits the book. Warmer voices for fiction; crisper voices for non-fiction.</p>

<h2>The practical conclusion</h2>
<p>If you're deciding between AI narration and not listening to the book, the answer is simple: AI narration. If you're deciding between AI narration and a professionally produced audiobook by a skilled narrator for a novel you love, the professional narrator will almost always be better.</p>
<p>But for the vast majority of books — particularly public domain classics, non-fiction, and any book that doesn't yet have a professionally produced version — AI narration in 2025 is genuinely good enough.</p>
<p>Hear it for yourself: <a href="/">listen to a sample on Freedible →</a></p>""",
    },
}

@app.get("/blog")
async def blog_index():
    cards = "".join(f"""
    <a class="blog-card" href="/blog/{slug}">
      <div class="blog-card-tag">{p['tag']}</div>
      <div class="blog-card-title">{p['title']}</div>
      <div class="blog-card-desc">{p['desc']}</div>
    </a>""" for slug, p in _BLOG_POSTS.items())
    return _page("Blog — Freedible",
                 "Articles on public domain audiobooks, AI narration, accessibility, and listening well.",
                 canonical="/blog", body=f"""
<h1>Freedible Blog</h1>
<p class="lead">Guides, recommendations, and thinking on audiobooks, AI narration, and accessibility.</p>
<div class="blog-grid">{cards}</div>""")

@app.get("/blog/{slug}")
async def blog_post(slug: str):
    post = _BLOG_POSTS.get(slug)
    if not post:
        raise HTTPException(404, "Post not found")
    bc = f'<nav class="bc"><a href="/">Home</a> <span>/</span> <a href="/blog">Blog</a> <span>/</span> {post["tag"]}</nav>'
    return _page(f"{post['title']} — Freedible", post["desc"], bc + post["body"], f"/blog/{slug}")

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

# ── Library seeding ───────────────────────────────────────────────────────────
async def _fetch_ol_cover(title: str, author: str) -> bytes | None:
    """Fetch a high-quality cover image from Open Library. Returns bytes or None."""
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get("https://openlibrary.org/search.json", params={
                "title": title, "author": author, "fields": "cover_i", "limit": 1
            })
            docs = r.json().get("docs", [])
            if not docs or "cover_i" not in docs[0]:
                return None
            cid = docs[0]["cover_i"]
            r2 = await client.get(f"https://covers.openlibrary.org/b/id/{cid}-L.jpg")
            if r2.status_code == 200 and len(r2.content) > 2000:
                return r2.content
    except Exception as e:
        print(f"[SEED] OL cover fetch failed for '{title}': {e}")
    return None

async def _seed_one(gut_id: int, title: str, author: str, voice: str) -> dict:
    """Download, parse, and insert one Gutenberg book. Returns result dict."""
    # Skip duplicates
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT id FROM books WHERE title=? AND author=?", (title, author)) as c:
            if await c.fetchone():
                return {"status": "exists", "title": title}

    # Download ePub
    url = f"https://www.gutenberg.org/cache/epub/{gut_id}/pg{gut_id}.epub"
    try:
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
    except Exception as e:
        return {"status": "error", "title": title, "reason": f"download: {e}"}

    bid = str(uuid.uuid4())
    try:
        parsed = _parse_epub(data, bid)
    except Exception as e:
        return {"status": "error", "title": title, "reason": f"parse: {e}"}

    chapters  = parsed["chapters"]
    cover_url = parsed["cover_url"]

    # Try Open Library for a nicer cover
    ol_bytes = await _fetch_ol_cover(title, author)
    if ol_bytes:
        cover_path = COVERS / f"{bid}.jpg"
        cover_path.write_bytes(ol_bytes)
        cover_url = f"/covers/{bid}.jpg"

    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT INTO books (id,title,author,cover,total,done,status,voice,created,user_id,visibility,rights_attestation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (bid, title, author, cover_url, len(chapters), 0, "uploaded",
             voice, time.time(), "__seed__", "public", 1))
        for i, ch in enumerate(chapters):
            cid = str(uuid.uuid4())
            await db.execute("INSERT INTO chapters VALUES (?,?,?,?,?,?,?)",
                (cid, bid, i+1, ch["title"], len(ch["text"].split()), "", "pending"))
            await db.execute("INSERT INTO texts VALUES (?,?)", (cid, ch["text"]))
        await db.commit()

    print(f"[SEED] ✓ {title} — {len(chapters)} chapters, cover={'OL' if ol_bytes else 'epub'}")
    return {"status": "seeded", "id": bid, "title": title, "chapters": len(chapters)}

async def _seed_all_task():
    print(f"[SEED] Starting batch seed of {len(_SEED_CATALOG)} books")
    for gut_id, title, author, voice in _SEED_CATALOG:
        result = await _seed_one(gut_id, title, author, voice)
        print(f"[SEED] {result}")
        await asyncio.sleep(3)  # gentle rate-limit on Gutenberg
    print("[SEED] Batch complete")

@app.post("/api/admin/seed")
async def admin_seed_one(request: Request, background_tasks: BackgroundTasks,
                         gut_id: int = Form(...)):
    await require_admin(request)
    entry = next((e for e in _SEED_CATALOG if e[0] == gut_id), None)
    if not entry:
        raise HTTPException(400, f"Gutenberg ID {gut_id} not in seed catalog")
    background_tasks.add_task(_seed_one, *entry)
    return {"status": "started", "title": entry[1]}

@app.post("/api/admin/seed-all")
async def admin_seed_all(request: Request, background_tasks: BackgroundTasks):
    await require_admin(request)
    background_tasks.add_task(_seed_all_task)
    return {"status": "started", "books": len(_SEED_CATALOG)}

@app.get("/api/admin/catalog")
async def admin_catalog(request: Request):
    await require_admin(request)
    return [{"gut_id": e[0], "title": e[1], "author": e[2], "voice": e[3]} for e in _SEED_CATALOG]

@app.post("/api/books/{bid}/generate")
async def generate(request: Request, bid: str, background_tasks: BackgroundTasks, voice: str = "af_bella"):
    user = await require_user(request)
    is_admin = (user.get("email") or "").lower() == ADMIN_EMAIL
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT status,user_id FROM books WHERE id=?", (bid,)) as c:
            book = await c.fetchone()
        if not book: raise HTTPException(404)
        if book["user_id"] != user["id"] and not is_admin: raise HTTPException(403, "Not your book")
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
