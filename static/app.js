// ── State ─────────────────────────────────────────────────────────────────────
const state = {
  page: 'library',
  books: [],
  book: null,
  player: null,         // { book, chapters, chapterId }
  pollTimer: null,
  bookId: null,
  user: null,
};

async function loadUser() {
  try {
    const r = await api('GET', '/api/auth/me');
    state.user = r.user || null;
  } catch { state.user = null; }
  renderHeader();
}
function signIn()  { location.href = '/api/auth/google'; }
async function signOut() {
  try { await api('POST', '/api/auth/logout'); } catch {}
  state.user = null;
  navigate('library'); push('#/');
  renderHeader();
}

function renderHeader() {
  const right = document.getElementById('header-right');
  if (!right) return;
  if (state.user) {
    const initial = (state.user.name || state.user.email || '?').trim().charAt(0).toUpperCase();
    const img = state.user.picture
      ? `<img src="${state.user.picture}" alt="" referrerpolicy="no-referrer"/>`
      : `<span>${initial}</span>`;
    right.innerHTML = `
      <button class="btn btn-primary" onclick="openUpload()">Add a Book</button>
      <div class="user-menu" onclick="toggleUserMenu(event)">
        <div class="user-avatar">${img}</div>
        <div class="user-dropdown" id="user-dropdown">
          <div class="user-dd-name">${esc(state.user.name || '')}</div>
          <div class="user-dd-email">${esc(state.user.email || '')}</div>
          <div class="user-dd-sep"></div>
          <button class="user-dd-item" onclick="signOut()">Sign out</button>
          ${state.user.is_admin ? '<button class="user-dd-item" onclick="openAdmin()">Admin · Reports</button>' : ''}
        </div>
      </div>`;
  } else {
    right.innerHTML = `<button class="btn btn-primary" onclick="signIn()">Sign in</button>`;
  }
}
function toggleUserMenu(e) {
  e.stopPropagation();
  document.getElementById('user-dropdown')?.classList.toggle('open');
}
document.addEventListener('click', () => {
  document.getElementById('user-dropdown')?.classList.remove('open');
});

const audio = new Audio();
audio.preload = 'auto';
let audioPlaying = false;
let audioLoading = false;
let showChapPanel = false;
let draggingSeek = false;
const SPEEDS = [0.75, 1, 1.25, 1.5, 1.75, 2];
let speedIdx = 1;

// ── Router ────────────────────────────────────────────────────────────────────
function navigate(page, id) {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
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

function push(hash) { history.pushState({}, '', hash); }

// ── API ───────────────────────────────────────────────────────────────────────
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (!r.ok) {
    const err = new Error(await r.text());
    err.status = r.status;
    throw err;
  }
  return r.json();
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmt(s) {
  if (!s || isNaN(s)) return '0:00';
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
function fmtEta(sec) {
  if (!sec || sec < 0) return '';
  if (sec < 60) return `~${sec}s`;
  if (sec < 3600) return `~${Math.round(sec/60)} min`;
  const h = Math.floor(sec/3600), m = Math.round((sec%3600)/60);
  return `~${h}h ${m}m`;
}
function wordTime(w) {
  const m = Math.round(w / 150);
  return m < 60 ? `~${m}m` : `~${Math.floor(m/60)}h ${m%60}m`;
}
function statusBadge(s) {
  return {
    uploaded:   '<span class="badge b-blue">Processing</span>',
    generating: '<span class="badge b-orange">Generating…</span>',
    complete:   '<span class="badge b-green">Ready</span>',
    error:      '<span class="badge b-red">Error</span>',
  }[s] || `<span class="badge b-blue">${s}</span>`;
}

// ── Library ───────────────────────────────────────────────────────────────────
async function renderLibrary() {
  document.title = 'Freedible — Listen to any book, free';

  const [publicBooks, myBooks, stats] = await Promise.all([
    api('GET', '/api/books?scope=public').catch(() => []),
    state.user ? api('GET', '/api/books?scope=mine').catch(() => []) : Promise.resolve([]),
    api('GET', '/api/stats').catch(() => ({ books: 0, hours: 0 })),
  ]);
  state.books = [...publicBooks, ...myBooks.filter(b => !publicBooks.some(p => p.id === b.id))];

  const heroCTA = state.user
    ? `<button class="btn btn-primary" onclick="openUpload()">Upload a Book</button>`
    : `<button class="btn btn-primary" onclick="signIn()">Sign in to upload</button>`;

  const hero = `
    <div class="landing-split">
      <div class="landing-text">
        <h1>Listen to any book, free.</h1>
        <p>Upload any PDF you own and we'll narrate it privately, just for you. Or browse the community library of public domain classics shared by listeners.</p>
        ${heroCTA}
      </div>
      <div class="landing-shelf" aria-hidden="true">
        ${shelfTile('Pride and Prejudice','Jane Austen','Novel','https://covers.openlibrary.org/b/olid/OL66550W-L.jpg')}
        ${shelfTile('Sherlock Holmes','Arthur Conan Doyle','Mystery','https://covers.openlibrary.org/b/olid/OL27516W-L.jpg')}
        ${shelfTile('The Art of War','Sun Tzu','Classic','https://covers.openlibrary.org/b/olid/OL8193949W-L.jpg')}
        ${shelfTile('Frankenstein','Mary Shelley','Gothic','https://covers.openlibrary.org/b/olid/OL2068538W-L.jpg')}
        ${shelfTile('Meditations','Marcus Aurelius','Philosophy','https://covers.openlibrary.org/b/olid/OL5676456W-L.jpg')}
      </div>
    </div>`;

  const mineSection = state.user && myBooks.length ? `
    <section class="lib-section">
      <div class="lib-head">
        <h2 class="lib-h1">Your Books</h2>
        <span class="lib-meta">${myBooks.length} book${myBooks.length !== 1 ? 's' : ''}</span>
      </div>
      <div class="grid" id="grid-mine">${myBooks.map(bookCard).join('')}</div>
    </section>` : '';

  const communitySection = `
    <section class="lib-section">
      <div class="lib-head">
        <h2 class="lib-h1">Community Library</h2>
        ${stats.books > 0 ? `<span class="lib-meta">${stats.books} book${stats.books !== 1 ? 's' : ''} · ${stats.hours}+ hrs of audio</span>` : ''}
      </div>
      ${publicBooks.length
        ? `<div class="grid" id="grid-public">${publicBooks.map(bookCard).join('')}</div>`
        : `<div class="empty-state">No public books yet. ${state.user ? 'Be the first to share one.' : 'Sign in to upload.'}</div>`}
    </section>`;

  document.getElementById('app').innerHTML = `
    <div class="page">
      ${hero}
      ${mineSection}
      ${communitySection}
    </div>`;

  if (state.books.some(b => b.status === 'generating')) {
    state.pollTimer = setInterval(async () => {
      const [fp, fm] = await Promise.all([
        api('GET', '/api/books?scope=public').catch(() => null),
        state.user ? api('GET', '/api/books?scope=mine').catch(() => null) : Promise.resolve([]),
      ]);
      if (!fp) return;
      state.books = [...fp, ...(fm || []).filter(b => !fp.some(p => p.id === b.id))];
      if (state.page === 'library') {
        const pub = document.getElementById('grid-public');
        const mine = document.getElementById('grid-mine');
        if (pub) pub.innerHTML = fp.map(bookCard).join('');
        if (mine && fm) mine.innerHTML = fm.map(bookCard).join('');
      }
      if (!state.books.some(b => b.status === 'generating')) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
      }
    }, 2500);
  }
}

function shelfTile(title, author, kicker, coverUrl) {
  const cover = coverUrl
    ? `<img class="tile-cover" src="${coverUrl}" alt="${esc(title)}" loading="lazy" onerror="this.style.display='none'"/>`
    : '';
  return `<div class="tile">
    ${cover}
    <div class="tile-top">${esc(kicker)}</div>
    <div class="tile-bottom">
      <div class="tile-title">${esc(title)}</div>
      <div class="tile-author">${esc(author)}</div>
    </div>
  </div>`;
}

function bookCard(b) {
  const pct = b.total ? Math.round(b.done / b.total * 100) : 0;
  const progBar = b.status === 'generating'
    ? `<div class="prog-track" style="margin-top:8px"><div class="prog-fill" style="width:${pct}%"></div></div>
       <div style="color:var(--muted);font-size:11px;margin-top:3px">${pct}% · ${b.done}/${b.total} chapters</div>` : '';
  const img = b.cover
    ? `<img src="${b.cover}" alt="" loading="lazy" style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover"/>`
    : `<div class="book-cover-placeholder">${bookSvg(38)}</div>`;
  const overlay = b.status === 'complete'
    ? `<div class="play-ov"><div class="play-ov-btn">${playIcon()}</div></div>` : '';
  const visBadge = b.visibility === 'private'
    ? `<div class="vis-badge" title="Private — only you can see this">Private</div>` : '';
  return `
    <div class="book-card" onclick="navigate('book','${b.id}');push('#/book/${b.id}')">
      <div class="book-cover">${img}${overlay}<div class="cover-badge">${statusBadge(b.status)}</div>${visBadge}</div>
      <div class="book-title">${esc(b.title)}</div>
      ${b.author ? `<div class="book-author">${esc(b.author)}</div>` : ''}
      ${progBar}
    </div>`;
}

// ── Upload ────────────────────────────────────────────────────────────────────
let uploadFile = null;
function openUpload() {
  if (!state.user) { signIn(); return; }
  uploadFile = null;
  document.getElementById('app').insertAdjacentHTML('beforeend', `
    <div class="overlay" id="upload-overlay" onclick="closeUploadIfBg(event)">
      <div class="modal">
        <div class="modal-header">
          <h2>Add a Book</h2>
          <button class="ibtn" onclick="closeUpload()">${closeIcon()}</button>
        </div>
        <div class="modal-note">
          Your upload is <strong>private by default</strong> — only you can see it. You can choose to share it with the community later if it's public domain or yours to share.
        </div>
        <div class="drop-zone" id="drop-zone"
             ondragover="dzDrag(event)" ondragleave="dzLeave(event)" ondrop="dzDrop(event)"
             onclick="document.getElementById('file-input').click()">
          <div class="drop-icon">${uploadSvg()}</div>
          <div class="drop-title" id="drop-title">Drop your PDF here</div>
          <div class="drop-sub" id="drop-sub">or click to browse · PDF only</div>
        </div>
        <div class="error-msg" id="upload-err" style="display:none"></div>
        <div class="modal-actions">
          <button class="btn btn-ghost" onclick="closeUpload()">Cancel</button>
          <button class="btn btn-primary" id="upload-btn" onclick="doUpload()" disabled>Add Privately</button>
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
  e.preventDefault(); dzLeave(e);
  const f = e.dataTransfer.files[0];
  if (f) fileChosen(f);
}
function fileChosen(f) {
  if (!f || !f.name.endsWith('.pdf')) return;
  uploadFile = f;
  const dz = document.getElementById('drop-zone');
  dz.classList.add('has-file'); dz.classList.remove('dragover');
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
    const r = await api('POST', '/api/upload', fd);
    closeUpload();
    navigate('book', r.id); push('#/book/' + r.id);
  } catch(e) {
    const el = document.getElementById('upload-err');
    el.textContent = e.message || 'Upload failed'; el.style.display = '';
    btn.disabled = false; btn.textContent = 'Add Privately';
  }
}

// ── Book Detail ───────────────────────────────────────────────────────────────
async function loadBook(id) {
  const book = await api('GET', `/api/books/${id}`).catch(() => null);
  if (!book) { navigate('library'); push('#/'); return; }
  state.book = book;
  document.title = `${book.title} — Freedible`;
  renderBook();
  if (book.status === 'generating') startPoll(id);
}

let _voices = [];
async function loadVoices() {
  try { _voices = await api('GET', '/api/voices'); } catch(e) { _voices = []; }
}
loadVoices();

function splitChapters(b) {
  const readable = [], skipped = [];
  for (const c of b.chapters) {
    if (c.status === 'complete' && !c.audio) skipped.push(c);
    else readable.push(c);
  }
  return { readable, skipped };
}

function shareBook() {
  const url = location.origin + '/#/book/' + state.book.id;
  navigator.clipboard.writeText(url).then(() => {
    const btn = document.getElementById('share-btn');
    if (btn) { btn.textContent = 'Link copied'; setTimeout(() => { btn.textContent = 'Share'; }, 2000); }
  });
}

async function publishBook(makePublic) {
  if (makePublic) {
    const ok = confirm(
      "Publish to the Community Library?\n\n" +
      "By publishing, you confirm either:\n" +
      "  • This book is in the public domain, OR\n" +
      "  • You own the copyright / have permission to share it.\n\n" +
      "Anyone can listen once published. Freedible will remove books reported as infringing."
    );
    if (!ok) return;
  }
  const fd = new FormData();
  fd.append('visibility', makePublic ? 'public' : 'private');
  fd.append('attest', makePublic ? 'true' : 'false');
  try {
    await api('POST', `/api/books/${state.book.id}/publish`, fd);
    await loadBook(state.book.id);
  } catch (e) {
    alert(e.message || 'Failed to update visibility');
  }
}

async function reportBook() {
  const reason = prompt("Why are you reporting this book? (copyright, harmful content, etc.)");
  if (reason === null) return;
  try {
    const fd = new FormData(); fd.append('reason', reason || '');
    await api('POST', `/api/books/${state.book.id}/report`, fd);
    alert("Thanks — we'll review this shortly.");
  } catch (e) {
    alert(e.message || 'Failed to submit report');
  }
}

async function openAdmin() {
  try {
    const reports = await api('GET', '/api/admin/reports');
    const rows = reports.length
      ? reports.map(r => `
          <div class="admin-row">
            <div>
              <div class="admin-row-title">${esc(r.book_title || '(deleted)')}</div>
              <div class="admin-row-meta">${esc(r.reporter_email || 'anonymous')} · ${new Date(r.created*1000).toLocaleString()} · <span class="admin-status-${r.status}">${r.status}</span></div>
              <div class="admin-row-reason">${esc(r.reason || '')}</div>
            </div>
            <button class="btn btn-danger" onclick="takedown('${r.book_id}')">Make Private</button>
          </div>`).join('')
      : '<div class="empty-state">No reports.</div>';
    document.getElementById('app').insertAdjacentHTML('beforeend', `
      <div class="overlay" id="admin-overlay" onclick="if(event.target.id==='admin-overlay')this.remove()">
        <div class="modal" style="max-width:720px">
          <div class="modal-header"><h2>Reports</h2><button class="ibtn" onclick="document.getElementById('admin-overlay').remove()">${closeIcon()}</button></div>
          <div class="admin-list">${rows}</div>
        </div>
      </div>`);
  } catch (e) { alert(e.message); }
}

async function takedown(bid) {
  if (!confirm('Make this book private (removed from public library)?')) return;
  try {
    await api('POST', `/api/admin/takedown/${bid}`);
    document.getElementById('admin-overlay')?.remove();
    openAdmin();
  } catch (e) { alert(e.message); }
}

function renderBook(progData) {
  const b = state.book;
  const { readable, skipped } = splitChapters(b);
  const ready = readable.filter(c => c.status === 'complete' && c.audio);
  const totalWords = b.chapters.reduce((a, c) => a + (c.words || 0), 0);
  const pct = b.total ? Math.round(b.done / b.total * 100) : 0;
  const curVoice = b.voice || '';

  const voiceOptions = _voices.length
    ? _voices.map(v => `<option value="${esc(v.id)}"${v.id === curVoice ? ' selected' : ''}>${esc(v.name)}</option>`).join('')
    : `<option value="">Default Voice</option>`;

  const firstReadyId = ready[0]?.id;

  const genControls = (() => {
    if (b.status === 'uploaded' || b.status === 'complete' || b.status === 'error') {
      return `<div class="voice-row">
        <select class="voice-select" id="voice-sel">${voiceOptions}</select>
        <button class="btn btn-primary" onclick="startGen()">${b.status === 'complete' ? 'Re-generate' : 'Generate Audiobook'}</button>
        ${firstReadyId ? `<button class="btn btn-primary" onclick="playChapter('${firstReadyId}')" style="background:var(--success)">▶ Play</button>` : ''}
      </div>`;
    }
    if (b.status === 'generating') {
      const currentChapter = progData?.current || '';
      const etaStr = progData?.eta ? fmtEta(progData.eta) : '';
      return `<div class="gen-card">
        <div class="gen-head">
          <div class="gen-pulse"></div>
          <div class="gen-head-text">
            <div class="gen-head-title">Generating Audiobook</div>
            <div class="gen-head-sub" id="gen-sub">Chapter ${b.done + 1} of ${b.total}${currentChapter ? ' — ' + esc(currentChapter) : ''}</div>
          </div>
        </div>
        <div class="prog-track gen-track"><div class="prog-fill" id="gen-fill" style="width:${pct}%"></div></div>
        <div class="gen-stats">
          <span id="gen-pct">${pct}% complete</span>
          ${etaStr ? `<span id="gen-eta">${etaStr} remaining</span>` : '<span id="gen-eta"></span>'}
        </div>
        ${firstReadyId
          ? `<button class="btn btn-primary gen-play-btn" onclick="playChapter('${firstReadyId}')">▶ Start listening · ${ready.length} chapter${ready.length !== 1 ? 's' : ''} ready</button>`
          : '<div class="gen-hint">First chapter ready in about a minute.</div>'}
      </div>`;
    }
    return '';
  })();

  const chapRows = readable.map(ch => {
    const isReady = ch.status === 'complete' && ch.audio;
    const isGenerating = progData?.current && progData.current === ch.title && ch.status === 'pending';
    const isActive = state.player && state.player.book.id === b.id && state.player.chapterId === ch.id;
    const cls = ['chap-row', isReady ? 'clickable' : 'dimmed', isActive ? 'active' : '', isGenerating ? 'generating-now' : ''].filter(Boolean).join(' ');
    const num = isActive
      ? `<div class="chap-num playing">${audioLoading ? spinnerSmall() : '♪'}</div>`
      : `<div class="chap-num">${isReady ? '▶' : (isGenerating ? spinnerSmall() : '…')}</div>`;
    const badge = isGenerating
      ? '<span class="badge b-orange">Generating…</span>'
      : { complete: '<span class="badge b-green">Ready</span>', pending: '<span class="badge b-blue">Pending</span>', error: '<span class="badge b-red">Error</span>' }[ch.status] || '';
    return `<div class="${cls}" ${isReady ? `onclick="playChapter('${ch.id}')"` : ''}>
      ${num}
      <div class="chap-body">
        <div class="chap-title${isActive ? ' active-text' : ''}">${esc(ch.title)}</div>
        <div class="chap-sub">${(ch.words||0).toLocaleString()} words · ${wordTime(ch.words||0)}</div>
      </div>
      ${badge}
    </div>`;
  }).join('');

  const skippedNote = skipped.length
    ? `<div class="skipped-note">${skipped.length} page${skipped.length !== 1 ? 's' : ''} skipped (copyright / front matter)</div>`
    : '';

  const totalTime = wordTime(totalWords);

  const isOwner = !!b.is_owner;
  const isAdmin = state.user && state.user.is_admin;
  const visBadgeLabel = b.visibility === 'public' ? 'Public' : 'Private';
  const visBadgeCls = b.visibility === 'public' ? 'b-green' : 'b-blue';

  const ownerActions = isOwner ? (
    b.visibility === 'public'
      ? `<button class="btn btn-ghost" style="font-size:13px;padding:7px 16px" onclick="publishBook(false)">Make Private</button>`
      : `<button class="btn" style="font-size:13px;padding:7px 16px;background:var(--accent);color:#fff" onclick="publishBook(true)">Publish to Community</button>`
  ) : '';

  const reportAction = (!isOwner && b.visibility === 'public' && state.user)
    ? `<button class="btn btn-ghost" style="font-size:13px;padding:7px 16px;color:var(--danger)" onclick="reportBook()">Report</button>` : '';

  const shareAction = b.visibility === 'public'
    ? `<button id="share-btn" class="btn btn-ghost" style="font-size:13px;padding:7px 16px" onclick="shareBook()">Share</button>` : '';

  const dangerZone = (isOwner || isAdmin) ? `
      <div class="danger-zone">
        <button class="btn btn-danger" onclick="confirmDelete()">${isOwner ? 'Delete Book' : 'Admin Delete'}</button>
        <span id="del-confirm" style="display:none">
          <span style="color:var(--muted);font-size:14px">Delete permanently?</span>
          <button class="btn" style="background:var(--danger);color:#fff;padding:7px 16px;margin-left:10px" onclick="doDelete()">Yes, Delete</button>
          <button class="btn btn-ghost" style="margin-left:6px" onclick="document.getElementById('del-confirm').style.display='none'">Cancel</button>
        </span>
      </div>` : '';

  document.getElementById('app').innerHTML = `
    <div class="page">
      <button class="btn btn-ghost back-btn" onclick="navigate('library');push('#/')">← Library</button>
      <div class="hero">
        <div class="hero-cover">
          ${b.cover ? `<img src="${b.cover}" alt=""/>` : `<div class="hero-cover-ph">${bookSvg(52)}</div>`}
        </div>
        <div class="hero-info">
          <div class="hero-badge">
            ${statusBadge(b.status)}
            <span class="badge ${visBadgeCls}" style="margin-left:6px">${visBadgeLabel}</span>
          </div>
          <h1 class="hero-title">${esc(b.title)}</h1>
          ${b.author ? `<div class="hero-author">${esc(b.author)}</div>` : ''}
          <div class="hero-meta">${b.total} chapters · ${totalWords.toLocaleString()} words · ${totalTime} listening</div>
          ${isOwner ? genControls : ''}
          <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
            ${shareAction}
            ${ownerActions}
            ${reportAction}
          </div>
        </div>
      </div>
      <div class="section-label">Chapters</div>
      <div class="chapters">${chapRows}</div>
      ${skippedNote}
      ${dangerZone}
    </div>`;
}

function confirmDelete() {
  document.getElementById('del-confirm').style.display = 'flex';
  document.getElementById('del-confirm').style.alignItems = 'center';
}
async function doDelete() {
  const deletedId = state.book.id;
  if (state.player && state.player.book.id === deletedId) {
    try { audio.pause(); } catch {}
    audio.removeAttribute('src');
    audio.load();
    state.player = null;
    const pl = document.getElementById('player');
    if (pl) pl.classList.add('hidden');
  }
  await api('DELETE', `/api/books/${deletedId}`);
  navigate('library'); push('#/');
}

async function startGen() {
  const voice = document.getElementById('voice-sel')?.value || 'af_bella';
  await api('POST', `/api/books/${state.book.id}/generate?voice=${encodeURIComponent(voice)}`);
  await loadBook(state.book.id);
}

let lastProg = {};
function startPoll(id) {
  clearInterval(state.pollTimer);
  state.pollTimer = setInterval(async () => {
    const prog = await api('GET', `/api/books/${id}/progress`).catch(e => {
      if (e && e.status === 404) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        navigate('library'); push('#/');
      }
      return null;
    });
    if (!prog) return;
    if (prog.status === 'complete' || prog.status === 'error') {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
      await loadBook(id);
      return;
    }

    const pct = prog.total ? Math.round(prog.done / prog.total * 100) : 0;
    const fill = document.getElementById('gen-fill');
    const pctEl = document.getElementById('gen-pct');
    const etaEl = document.getElementById('gen-eta');
    const subEl = document.getElementById('gen-sub');
    if (fill) fill.style.width = pct + '%';
    if (pctEl) pctEl.textContent = pct + '% complete';
    if (etaEl) etaEl.textContent = prog.eta ? fmtEta(prog.eta) + ' remaining' : '';
    if (subEl && prog.current) subEl.textContent = `Chapter ${prog.done + 1} of ${prog.total} — ${prog.current}`;

    const fresh = await api('GET', `/api/books/${id}`).catch(() => null);
    if (!fresh || state.page !== 'book' || state.bookId !== id) return;
    const prevReady = state.book?.chapters.filter(c => c.status === 'complete' && c.audio).length || 0;
    const newReady = fresh.chapters.filter(c => c.status === 'complete' && c.audio).length;
    state.book = fresh;

    if (state.player && state.player.book.id === id) {
      state.player.chapters = fresh.chapters.filter(c => c.status === 'complete' && c.audio);
      state.player.book = fresh;
    }

    const currentChanged = (lastProg.current || '') !== (prog.current || '');
    if (newReady !== prevReady || currentChanged) {
      lastProg = prog;
      const scrollY = window.scrollY;
      renderBook(prog);
      requestAnimationFrame(() => window.scrollTo(0, scrollY));
    }
  }, 2000);
}

// ── Player ────────────────────────────────────────────────────────────────────
function playChapter(chapterId) {
  const ch = state.book.chapters.find(c => c.id === chapterId);
  if (!ch || !ch.audio) return;
  const ready = state.book.chapters.filter(c => c.status === 'complete' && c.audio);
  state.player = { book: state.book, chapters: ready, chapterId };
  loadAndPlay(ch);
  renderPlayerBar();
  if (state.page === 'book') renderBook(lastProg);
}

function currentPlayerChapter() {
  if (!state.player) return null;
  return state.player.chapters.find(c => c.id === state.player.chapterId) || null;
}

function loadAndPlay(ch) {
  audioLoading = true;
  updatePlayBtn();
  audio.src = ch.audio;
  audio.playbackRate = SPEEDS[speedIdx];
  audio.load();
  audio.play().catch(() => {});
  updatePlayerInfo();
}

audio.addEventListener('loadstart',  () => { audioLoading = true;  updatePlayBtn(); });
audio.addEventListener('waiting',    () => { audioLoading = true;  updatePlayBtn(); });
audio.addEventListener('canplay',    () => { audioLoading = false; updatePlayBtn(); });
audio.addEventListener('playing',    () => { audioLoading = false; audioPlaying = true; updatePlayBtn(); });
audio.addEventListener('play',       () => { audioPlaying = true;  updatePlayBtn(); });
audio.addEventListener('pause',      () => { audioPlaying = false; updatePlayBtn(); });
audio.addEventListener('error',      () => { audioLoading = false; updatePlayBtn(); });
audio.addEventListener('timeupdate', () => {
  if (draggingSeek) return;
  const pct = audio.duration ? audio.currentTime / audio.duration * 100 : 0;
  const fill = document.getElementById('seek-fill');
  const time = document.getElementById('player-time');
  if (fill) fill.style.width = pct + '%';
  if (time) time.textContent = fmt(audio.currentTime) + ' / ' + fmt(audio.duration);
});
audio.addEventListener('ended', () => {
  if (!state.player) return;
  const idx = state.player.chapters.findIndex(c => c.id === state.player.chapterId);
  const next = state.player.chapters[idx + 1];
  if (next) {
    state.player.chapterId = next.id;
    loadAndPlay(next);
    renderPlayerBar();
    if (state.page === 'book') renderBook(lastProg);
  } else {
    audioPlaying = false; updatePlayBtn();
  }
});

function updatePlayBtn() {
  const btn = document.getElementById('play-btn');
  if (!btn) return;
  btn.innerHTML = audioLoading ? spinnerLarge() : (audioPlaying ? pauseIcon() : playIcon());
}
function updatePlayerInfo() {
  const ch = currentPlayerChapter();
  const el = document.getElementById('player-chap');
  if (el) el.textContent = ch?.title || '';
}

function renderPlayerBar() {
  const ch = currentPlayerChapter();
  if (!ch) return;
  const { book } = state.player;
  const playerEl = document.getElementById('player');
  playerEl.classList.remove('hidden');
  playerEl.innerHTML = `
    <div class="player-seek" id="seek-track">
      <div class="player-seek-fill" id="seek-fill" style="width:0%"></div>
    </div>
    <div class="player-body">
      <div class="player-info">
        <div class="player-thumb">
          ${book.cover ? `<img src="${book.cover}" alt=""/>` : `<div class="player-thumb-ph">${bookSvg(18)}</div>`}
        </div>
        <div class="player-text">
          <div class="player-chap" id="player-chap">${esc(ch.title)}</div>
          <div class="player-book">${esc(book.title)}</div>
        </div>
      </div>
      <div class="player-controls">
        <div class="ctrl-row">
          <button class="ibtn" id="prev-btn" onclick="playerPrev()" title="Previous chapter">${prevIcon()}</button>
          <button class="ibtn" onclick="audio.currentTime=Math.max(0,audio.currentTime-30)" title="−30s">${skip30Icon('−')}</button>
          <button class="ibtn-lg" id="play-btn" onclick="togglePlay()">${audioLoading ? spinnerLarge() : (audioPlaying ? pauseIcon() : playIcon())}</button>
          <button class="ibtn" onclick="audio.currentTime=Math.min(audio.duration||0,audio.currentTime+30)" title="+30s">${skip30Icon('+')}</button>
          <button class="ibtn" id="next-btn" onclick="playerNext()" title="Next chapter">${nextIcon()}</button>
        </div>
        <div class="player-time" id="player-time">0:00 / 0:00</div>
      </div>
      <div class="player-right">
        <button class="speed-btn" onclick="cycleSpeed()">${SPEEDS[speedIdx]}×</button>
        <div class="vol-row">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="color:var(--muted);flex-shrink:0"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02z"/></svg>
          <input type="range" min="0" max="1" step="0.05" value="${audio.volume}" style="width:70px" onchange="audio.volume=+this.value"/>
        </div>
        <button class="ibtn clist-btn${showChapPanel?' active':''}" onclick="toggleChapPanel()" title="Chapters">${listIcon()}</button>
      </div>
    </div>`;

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
  if (!state.player) return;
  const idx = state.player.chapters.findIndex(c => c.id === state.player.chapterId);
  const prev = state.player.chapters[idx - 1];
  if (prev) playChapter(prev.id);
}
function playerNext() {
  if (!state.player) return;
  const idx = state.player.chapters.findIndex(c => c.id === state.player.chapterId);
  const next = state.player.chapters[idx + 1];
  if (next) playChapter(next.id);
}
function cycleSpeed() {
  speedIdx = (speedIdx + 1) % SPEEDS.length;
  audio.playbackRate = SPEEDS[speedIdx];
  document.querySelector('.speed-btn').textContent = SPEEDS[speedIdx] + '×';
}
function toggleChapPanel() {
  showChapPanel = !showChapPanel;
  document.getElementById('chap-panel')?.remove();
  document.querySelector('.clist-btn')?.classList.toggle('active', showChapPanel);
  if (showChapPanel) renderChapPanel();
}
function renderChapPanel() {
  const { chapters, chapterId } = state.player;
  const panel = document.createElement('div');
  panel.id = 'chap-panel';
  panel.className = 'chap-panel';
  panel.innerHTML = `
    <div class="chap-panel-head">Chapters</div>
    <div class="chap-panel-list">
      ${chapters.map(ch => `
        <div class="chap-panel-item${ch.id === chapterId ? ' active' : ''}" onclick="playChapter('${ch.id}');toggleChapPanel()">
          ${esc(ch.title)}
        </div>`).join('')}
    </div>`;
  document.body.appendChild(panel);
}

// ── Icons ─────────────────────────────────────────────────────────────────────
const bookSvg     = (sz=36) => `<svg width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="none" stroke="var(--border)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>`;
const closeIcon   = () => `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;
const hpIconLg    = () => `<svg width="60" height="60" viewBox="0 0 24 24" fill="none" stroke="var(--border)" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/><path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>`;
const hpIconSm    = () => `<svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/><path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>`;
const uploadSvg   = () => `<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--muted)" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>`;
const playIcon    = () => `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>`;
const pauseIcon   = () => `<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>`;
const prevIcon    = () => `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6h2v12H6zm3.5 6 8.5 6V6z"/></svg>`;
const nextIcon    = () => `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor"><path d="M6 18l8.5-6L6 6v12zm2.5-6 5.5 3.9V8.1L8.5 12zM16 6h2v12h-2z"/></svg>`;
const skip30Icon  = (d) => `<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor">
  ${d === '−'
    ? '<path d="M12 5V1L7 6l5 5V7c3.31 0 6 2.69 6 6s-2.69 6-6 6-6-2.69-6-6H4c0 4.42 3.58 8 8 8s8-3.58 8-8-3.58-8-8-8z"/>'
    : '<path d="M18 13c0 3.31-2.69 6-6 6s-6-2.69-6-6 2.69-6 6-6v4l5-5-5-5v4c-4.42 0-8 3.58-8 8s3.58 8 8 8 8-3.58 8-8h-2z"/>'}
  <text x="8" y="15.5" font-size="5" fill="currentColor" font-family="Inter,sans-serif" font-weight="700">30</text>
</svg>`;
const listIcon    = () => `<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M3 13h2v-2H3v2zm0 4h2v-2H3v2zm0-8h2V7H3v2zm4 4h14v-2H7v2zm0 4h14v-2H7v2zM7 7v2h14V7H7z"/></svg>`;
const spinnerLarge = () => `<svg class="spin" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 2a10 10 0 1 0 10 10"/></svg>`;
const spinnerSmall = () => `<svg class="spin" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M12 2a10 10 0 1 0 10 10"/></svg>`;

// ── Utils ─────────────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
  await loadUser();
  const m = location.hash.match(/^#\/book\/(.+)$/);
  if (m) navigate('book', m[1]);
  else renderLibrary();
})();
