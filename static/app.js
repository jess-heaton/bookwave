// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  page: 'library',      // 'library' | 'book'
  books: [],
  book: null,           // current book detail
  player: null,         // { book, chapters, idx }
  pollTimer: null,
};

const audio = new Audio();
let audioPlaying = false;
let showChapPanel = false;
let draggingSeek = false;
const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];
let speedIdx = 1;

// ── Router ────────────────────────────────────────────────────────────────────
function navigate(page, id) {
  clearInterval(state.pollTimer);
  state.page = page;
  state.bookId = id || null;
  if (page === 'library') renderLibrary();
  else if (page === 'book') loadBook(id);
}

window.addEventListener('popstate', () => {
  const m = location.hash.match(/^#\/book\/(.+)$/);
  if (m) navigate('book', m[1]);
  else navigate('library');
});

function push(hash) {
  history.pushState({}, '', hash);
}

// ── API ───────────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(s) {
  if (!s || isNaN(s)) return '0:00';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
function wordTime(w) {
  const m = Math.round(w / 150);
  return m < 60 ? `~${m}m` : `~${Math.floor(m/60)}h ${m%60}m`;
}
function statusBadge(s) {
  return {
    uploaded:   '<span class="badge b-blue">Not Generated</span>',
    generating: '<span class="badge b-orange">Generating…</span>',
    complete:   '<span class="badge b-green">Ready</span>',
    error:      '<span class="badge b-red">Error</span>',
  }[s] || `<span class="badge b-blue">${s}</span>`;
}
function cover(src, ph='📖', cls='') {
  return src
    ? `<img src="${src}" alt="" loading="lazy" class="${cls}"/>`
    : `<div class="hero-cover-ph ${cls}">${ph}</div>`;
}

// ── Library ───────────────────────────────────────────────────────────────────
async function renderLibrary() {
  document.title = 'Bookwave';
  const books = await api('GET', '/api/books').catch(() => []);
  state.books = books;

  const grid = books.length === 0
    ? `<div class="empty">
        <div class="empty-icon">📚</div>
        <h2>No books yet</h2>
        <p>Upload a PDF to get started</p>
        <button class="btn btn-primary" onclick="openUpload()">Upload your first book</button>
       </div>`
    : `<div class="grid">${books.map(bookCard).join('')}</div>`;

  document.getElementById('app').innerHTML = `
    <div class="page">
      <div class="header">
        <div class="logo">
          <span class="logo-icon">🎧</span>
          <div>
            <div class="logo-text">Bookwave</div>
            <div class="logo-sub">Your personal audiobook library</div>
          </div>
        </div>
        <button class="btn btn-primary" onclick="openUpload()">+ Add Book</button>
      </div>
      ${grid}
    </div>`;

  // Poll if any book is generating
  if (books.some(b => b.status === 'generating')) {
    state.pollTimer = setInterval(async () => {
      const fresh = await api('GET', '/api/books').catch(() => null);
      if (!fresh) return;
      state.books = fresh;
      if (state.page === 'library') {
        const gridEl = document.querySelector('.grid');
        if (gridEl) gridEl.innerHTML = fresh.map(bookCard).join('');
      }
      if (!fresh.some(b => b.status === 'generating')) clearInterval(state.pollTimer);
    }, 2500);
  }
}

function bookCard(b) {
  const pct = b.total ? Math.round(b.done / b.total * 100) : 0;
  const progBar = b.status === 'generating'
    ? `<div class="prog-track" style="margin-top:8px"><div class="prog-fill" style="width:${pct}%"></div></div>
       <div style="color:var(--muted);font-size:11px;margin-top:3px">${pct}%</div>` : '';
  const img = b.cover
    ? `<img src="${b.cover}" alt="" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover"/>`
    : `<div class="book-cover-placeholder">📖</div>`;
  return `
    <div class="book-card" onclick="navigate('book','${b.id}');push('#/book/${b.id}')">
      <div class="book-cover">${img}<div class="cover-badge">${statusBadge(b.status)}</div></div>
      <div class="book-title">${esc(b.title)}</div>
      ${b.author ? `<div class="book-author">${esc(b.author)}</div>` : ''}
      <div class="book-meta">${b.total} chapter${b.total !== 1 ? 's' : ''}</div>
      ${progBar}
    </div>`;
}

// ── Upload ────────────────────────────────────────────────────────────────────
let uploadFile = null;

function openUpload() {
  uploadFile = null;
  document.getElementById('app').insertAdjacentHTML('beforeend', `
    <div class="overlay" id="upload-overlay" onclick="closeUploadIfBg(event)">
      <div class="modal">
        <div class="modal-header">
          <h2>Add a Book</h2>
          <button class="ibtn" onclick="closeUpload()">✕</button>
        </div>
        <div class="drop-zone" id="drop-zone"
             ondragover="dzDrag(event)" ondragleave="dzLeave(event)" ondrop="dzDrop(event)"
             onclick="document.getElementById('file-input').click()">
          <div class="drop-icon">📚</div>
          <div class="drop-title" id="drop-title">Drop your PDF here</div>
          <div class="drop-sub" id="drop-sub">or click to browse</div>
        </div>
        <div class="error-msg" id="upload-err" style="display:none"></div>
        <div class="modal-actions">
          <button class="btn btn-ghost" onclick="closeUpload()">Cancel</button>
          <button class="btn btn-primary" id="upload-btn" onclick="doUpload()" disabled>Add Book</button>
        </div>
        <input type="file" id="file-input" accept=".pdf" style="display:none" onchange="fileChosen(this.files[0])"/>
      </div>
    </div>`);
}

function closeUploadIfBg(e) { if (e.target.id === 'upload-overlay') closeUpload(); }
function closeUpload() { document.getElementById('upload-overlay')?.remove(); }

function dzDrag(e) { e.preventDefault(); document.getElementById('drop-zone').classList.add('dragover'); }
function dzLeave(e) { document.getElementById('drop-zone').classList.remove('dragover'); }
function dzDrop(e) {
  e.preventDefault();
  dzLeave(e);
  const f = e.dataTransfer.files[0];
  if (f) fileChosen(f);
}

function fileChosen(f) {
  if (!f || !f.name.endsWith('.pdf')) return;
  uploadFile = f;
  const dz = document.getElementById('drop-zone');
  dz.classList.add('has-file');
  dz.classList.remove('dragover');
  document.getElementById('drop-title').textContent = f.name;
  document.getElementById('drop-sub').textContent = (f.size / 1024 / 1024).toFixed(1) + ' MB';
  document.getElementById('upload-btn').disabled = false;
}

async function doUpload() {
  if (!uploadFile) return;
  const btn = document.getElementById('upload-btn');
  btn.disabled = true; btn.textContent = 'Uploading…';
  document.getElementById('upload-err').style.display = 'none';
  try {
    const fd = new FormData(); fd.append('file', uploadFile);
    await api('POST', '/api/upload', fd);
    closeUpload();
    navigate('library');
  } catch(e) {
    const el = document.getElementById('upload-err');
    el.textContent = e.message || 'Upload failed'; el.style.display = '';
    btn.disabled = false; btn.textContent = 'Add Book';
  }
}

// ── Book Detail ───────────────────────────────────────────────────────────────
async function loadBook(id) {
  const book = await api('GET', `/api/books/${id}`).catch(() => null);
  if (!book) { navigate('library'); push('#/'); return; }
  state.book = book;
  document.title = `${book.title} — Bookwave`;
  renderBook();
  if (book.status === 'generating') startPoll(id);
}

// Voices loaded dynamically from /api/voices (real Windows voices)
let _voices = [];
async function loadVoices() {
  try { _voices = await api('GET', '/api/voices'); } catch(e) { _voices = []; }
}
loadVoices();

function renderBook() {
  const b = state.book;
  const ready = b.chapters.filter(c => c.status === 'complete' && c.audio);
  const totalWords = b.chapters.reduce((a, c) => a + (c.words || 0), 0);
  const pct = b.total ? Math.round(b.done / b.total * 100) : 0;
  const curVoice = b.voice || '';

  const voiceOptions = _voices.length
    ? _voices.map(v => `<option value="${esc(v.id)}"${v.id === curVoice ? ' selected' : ''}>${esc(v.name)}</option>`).join('')
    : `<option value="">Default Voice</option>`;

  const genControls = (() => {
    if (b.status === 'uploaded' || b.status === 'complete' || b.status === 'error') {
      return `<div class="voice-row">
        <select class="voice-select" id="voice-sel">${voiceOptions}</select>
        <button class="btn btn-primary" onclick="startGen()">🎙 ${b.status === 'complete' ? 'Re-generate' : 'Generate Audiobook'}</button>
        ${ready.length > 0 ? `<button class="btn btn-primary" onclick="playFrom(0)" style="background:var(--success)">▶ Play</button>` : ''}
      </div>`;
    }
    if (b.status === 'generating') {
      const playBtn = ready.length > 0
        ? `<button class="btn btn-primary" onclick="playFrom(0)" style="background:var(--success);margin-top:10px">▶ Play ${ready.length} ready chapter${ready.length !== 1 ? 's' : ''}</button>`
        : '';
      return `<div class="gen-progress">
        <div class="gen-label">Generating chapter ${b.done} of ${b.total}…</div>
        <div class="prog-track"><div class="prog-fill" id="gen-fill" style="width:${pct}%"></div></div>
        <div class="gen-pct" id="gen-pct">${pct}%</div>
        ${playBtn}
      </div>`;
    }
    return '';
  })();

  const chapRows = b.chapters.map((ch, i) => {
    const isReady = ch.status === 'complete' && ch.audio;
    const readyIdx = ready.findIndex(r => r.id === ch.id);
    const isActive = state.player && state.player.book.id === b.id &&
                     ready[state.player.idx]?.id === ch.id;
    const cls = [
      'chap-row',
      isReady ? 'clickable' : 'dimmed',
      isActive ? 'active' : ''
    ].filter(Boolean).join(' ');
    const num = isActive
      ? `<div class="chap-num playing">♪</div>`
      : `<div class="chap-num">${isReady ? '▶' : '…'}</div>`;
    const badge = {
      complete:   '<span class="badge b-green">Ready</span>',
      generating: '<span class="badge b-orange">…</span>',
      pending:    '<span class="badge b-blue">Pending</span>',
      error:      '<span class="badge b-red">Error</span>',
    }[ch.status] || '';
    return `<div class="${cls}" ${isReady ? `onclick="playFrom(${readyIdx})"` : ''}>
      ${num}
      <div class="chap-body">
        <div class="chap-title${isActive ? ' active-text' : ''}">${esc(ch.title)}</div>
        <div class="chap-sub">${(ch.words||0).toLocaleString()} words · ${wordTime(ch.words||0)}</div>
      </div>
      ${badge}
    </div>`;
  }).join('');

  document.getElementById('app').innerHTML = `
    <div class="page">
      <button class="btn btn-ghost back-btn" onclick="navigate('library');push('#/')">← Library</button>
      <div class="hero">
        <div class="hero-cover">
          ${b.cover ? `<img src="${b.cover}" alt=""/>` : '<div class="hero-cover-ph">📖</div>'}
        </div>
        <div class="hero-info">
          <div class="hero-badge">${statusBadge(b.status)}</div>
          <h1 class="hero-title">${esc(b.title)}</h1>
          ${b.author ? `<div class="hero-author">${esc(b.author)}</div>` : ''}
          <div class="hero-meta">${b.total} chapters · ${totalWords.toLocaleString()} words</div>
          ${genControls}
        </div>
      </div>
      <div class="section-label">Chapters</div>
      <div class="chapters">${chapRows}</div>
      <div class="danger-zone">
        <button class="btn btn-danger" onclick="confirmDelete()">Delete Book</button>
        <span id="del-confirm" style="display:none">
          <span style="color:var(--muted);font-size:14px">Are you sure?</span>
          <button class="btn" style="background:var(--danger);color:#fff;padding:7px 16px;margin-left:10px" onclick="doDelete()">Yes, Delete</button>
          <button class="btn btn-ghost" style="margin-left:6px" onclick="document.getElementById('del-confirm').style.display='none'">Cancel</button>
        </span>
      </div>
    </div>`;
}

function confirmDelete() {
  document.getElementById('del-confirm').style.display = 'flex';
  document.getElementById('del-confirm').style.alignItems = 'center';
}
async function doDelete() {
  await api('DELETE', `/api/books/${state.book.id}`);
  navigate('library'); push('#/');
}

async function startGen() {
  const voice = document.getElementById('voice-sel')?.value || 'af_bella';
  await api('POST', `/api/books/${state.book.id}/generate?voice=${encodeURIComponent(voice)}`);
  await loadBook(state.book.id);
}

function startPoll(id) {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    const prog = await api('GET', `/api/books/${id}/progress`).catch(() => null);
    if (!prog) return;
    // Update progress bar if visible
    const fill = document.getElementById('gen-fill');
    const pctEl = document.getElementById('gen-pct');
    const pct = prog.total ? Math.round(prog.done / prog.total * 100) : 0;
    if (fill) fill.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '%';
    if (prog.status === 'complete' || prog.status === 'error') {
      clearInterval(state.pollTimer);
      await loadBook(id);
    } else {
      // Re-render so newly-completed chapters become playable mid-generation
      const fresh = await api('GET', `/api/books/${id}`).catch(() => null);
      if (fresh && state.page === 'book' && state.bookId === id) {
        const prevReady = state.book?.chapters.filter(c => c.status === 'complete').length || 0;
        state.book = fresh;
        const newReady = fresh.chapters.filter(c => c.status === 'complete' && c.audio).length;
        if (newReady !== prevReady) renderBook();
      }
    }
  }, 2000);
}

function playFrom(idx) {
  const ready = state.book.chapters.filter(c => c.status === 'complete');
  if (!ready.length) return;
  state.player = { book: state.book, chapters: ready, idx };
  playIdx(idx);
  renderPlayerBar();
  if (state.page === 'book') renderBook();
}

// ── Audio player ──────────────────────────────────────────────────────────────
function playIdx(idx) {
  const { chapters } = state.player;
  if (idx < 0 || idx >= chapters.length) return;
  state.player.idx = idx;
  audio.src = chapters[idx].audio;
  audio.playbackRate = SPEEDS[speedIdx];
  audio.load();
  audio.play().then(() => { audioPlaying = true; updatePlayBtn(); }).catch(() => {});
  updatePlayerInfo();
}

audio.addEventListener('timeupdate', () => {
  if (draggingSeek) return;
  const pct = audio.duration ? audio.currentTime / audio.duration * 100 : 0;
  const fill = document.getElementById('seek-fill');
  const time = document.getElementById('player-time');
  if (fill) fill.style.width = pct + '%';
  if (time) time.textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
});
audio.addEventListener('play',  () => { audioPlaying = true;  updatePlayBtn(); });
audio.addEventListener('pause', () => { audioPlaying = false; updatePlayBtn(); });
audio.addEventListener('ended', () => {
  const { chapters, idx } = state.player;
  if (idx + 1 < chapters.length) playIdx(idx + 1);
  else audioPlaying = false;
});

function updatePlayBtn() {
  const btn = document.getElementById('play-btn');
  if (btn) btn.innerHTML = audioPlaying ? pauseIcon() : playIcon();
}
function updatePlayerInfo() {
  const ch = state.player.chapters[state.player.idx];
  const el = document.getElementById('player-chap');
  if (el) el.textContent = ch?.title || '';
}

function renderPlayerBar() {
  const { book, chapters, idx } = state.player;
  const ch = chapters[idx];
  const playerEl = document.getElementById('player');
  playerEl.classList.remove('hidden');
  playerEl.innerHTML = `
    <div class="player-seek" id="seek-track">
      <div class="player-seek-fill" id="seek-fill" style="width:0%"></div>
    </div>
    <div class="player-body">
      <div class="player-info">
        <div class="player-thumb">
          ${book.cover ? `<img src="${book.cover}" alt=""/>` : '<div class="player-thumb-ph">📖</div>'}
        </div>
        <div class="player-text">
          <div class="player-chap" id="player-chap">${esc(ch.title)}</div>
          <div class="player-book">${esc(book.title)}</div>
        </div>
      </div>
      <div class="player-controls">
        <div class="ctrl-row">
          <button class="ibtn" id="prev-btn" onclick="playerPrev()" title="Previous chapter">
            ${prevIcon()}
          </button>
          <button class="ibtn" onclick="audio.currentTime=Math.max(0,audio.currentTime-30)" title="−30s">
            ${skip30Icon('−')}
          </button>
          <button class="ibtn-lg" id="play-btn" onclick="togglePlay()">
            ${audioPlaying ? pauseIcon() : playIcon()}
          </button>
          <button class="ibtn" onclick="audio.currentTime=Math.min(audio.duration||0,audio.currentTime+30)" title="+30s">
            ${skip30Icon('+')}
          </button>
          <button class="ibtn" id="next-btn" onclick="playerNext()" title="Next chapter">
            ${nextIcon()}
          </button>
        </div>
        <div class="player-time" id="player-time">0:00 / 0:00</div>
      </div>
      <div class="player-right">
        <button class="speed-btn" onclick="cycleSpeed()">${SPEEDS[speedIdx]}×</button>
        <div class="vol-row">
          🔉
          <input type="range" min="0" max="1" step="0.05" value="${audio.volume}"
                 style="width:70px" onchange="audio.volume=+this.value"/>
        </div>
        <button class="ibtn clist-btn${showChapPanel?' active':''}" onclick="toggleChapPanel()" title="Chapters">
          ${listIcon()}
        </button>
      </div>
    </div>`;

  // Seek interaction
  const track = document.getElementById('seek-track');
  const seekTo = (e) => {
    const r = track.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    audio.currentTime = ratio * (audio.duration || 0);
    document.getElementById('seek-fill').style.width = (ratio * 100) + '%';
  };
  track.addEventListener('mousedown', e => { draggingSeek = true; seekTo(e); });
  document.addEventListener('mousemove', e => { if (draggingSeek) seekTo(e); });
  document.addEventListener('mouseup', () => { draggingSeek = false; });
  track.addEventListener('click', seekTo);
}

function togglePlay() {
  if (audioPlaying) audio.pause(); else audio.play();
}
function playerPrev() {
  if (state.player.idx > 0) playIdx(state.player.idx - 1);
}
function playerNext() {
  if (state.player.idx < state.player.chapters.length - 1) playIdx(state.player.idx + 1);
}
function cycleSpeed() {
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  audio.playbackRate = SPEEDS[speedIdx];
  document.querySelector('.speed-btn').textContent = SPEEDS[speedIdx] + '×';
}
function toggleChapPanel() {
  showChapPanel = !showChapPanel;
  const existing = document.getElementById('chap-panel');
  if (existing) { existing.remove(); }
  const btn = document.querySelector('.clist-btn');
  if (btn) btn.classList.toggle('active', showChapPanel);
  if (showChapPanel) renderChapPanel();
}
function renderChapPanel() {
  const { chapters, idx } = state.player;
  const panel = document.createElement('div');
  panel.id = 'chap-panel';
  panel.className = 'chap-panel';
  panel.innerHTML = `
    <div class="chap-panel-head">Chapters</div>
    <div class="chap-panel-list">
      ${chapters.map((ch, i) => `
        <div class="chap-panel-item${i === idx ? ' active' : ''}" onclick="jumpChap(${i})">
          ${esc(ch.title)}
        </div>`).join('')}
    </div>`;
  document.body.appendChild(panel);
}
function jumpChap(i) {
  playIdx(i);
  renderPlayerBar();
  toggleChapPanel();
  if (state.page === 'book') renderBook();
}

// ── Icons (inline SVG) ────────────────────────────────────────────────────────
const playIcon  = () => `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
const pauseIcon = () => `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;
const prevIcon  = () => `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>`;
const nextIcon  = () => `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zm2.5-6 5.5 3.9V8.1L8.5 12zM16 6h2v12h-2z"/></svg>`;
const skip30Icon = (d) => `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
  ${d === '−'
    ? '<path d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/>'
    : '<path d="M18 13c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6v4l5-5-5-5v4c-4.42 0-8 3.58-8 8s3.58 8 8 8 8-3.58 8-8h-2z"/>'}
  <text x="8" y="15.5" font-size="5" fill="currentColor" font-family="Inter,sans-serif" font-weight="700">30</text>
</svg>`;
const listIcon = () => `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/></svg>`;

// ── Utils ─────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
const m = location.hash.match(/^#\/book\/(.+)$/);
if (m) navigate('book', m[1]);
else renderLibrary();
