
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  let _courseId = null, _lang = null, _books = [];
  let _busy = false;

  async function boot() {
    const { course } = await fetch('/api/courses/active').then(r => r.json()).catch(() => ({}));
    if (!course) {
      document.getElementById('tb-body').innerHTML =
        `<div class="tb-empty"><h2>No course yet</h2>
         <p>Start a learning course first, then upload textbooks for that language.</p>
         <a class="tb-btn" href="/learn" style="display:inline-block;margin-top:10px;text-decoration:none">Go to Learn</a></div>`;
      return;
    }
    _courseId = course.id; _lang = course.target_lang;
    await loadBooks();
  }

  async function loadBooks() {
    try {
      const r = await fetch(`/api/courses/${_courseId}/textbooks`);
      const data = await r.json();
      _books = data.textbooks || [];
    } catch (e) { _books = []; }
    renderLibrary();
  }

  function _pct(b) { return b.num_pages ? Math.round(100 * (b.last_page || 0) / b.num_pages) : 0; }

  function renderLibrary() {
    const wrap = document.getElementById('tb-body');
    const uploadCard = `<button class="tb-upload-card" onclick="pickFile()">
      <span class="plus">＋</span><span>Upload a textbook PDF</span></button>`;
    if (!_books.length) {
      wrap.innerHTML = `<div class="tb-grid">${uploadCard}</div>
        <div class="tb-empty" style="padding-top:24px"><p>No textbooks yet. Upload a PDF to start reading it by chapter.</p></div>`;
      return;
    }
    const cards = _books.map(b => {
      const pct = _pct(b);
      const cover = b.has_pdf
        ? `<img src="/api/textbooks/${b.id}/page/1.jpg" alt="" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'tb-cover-fallback',textContent:${JSON.stringify((b.title||'?').slice(0,1))},style:'background:var(--primary)'}))">`
        : `<div class="tb-cover-fallback" style="background:var(--primary)">${esc((b.title || '?').slice(0, 1))}</div>`;
      const badge = b.has_pdf ? '' : '<span class="tb-cover-badge">text only</span>';
      const ch = (b.chapters || []).length;
      // Chapter detection is an AI call that can take a while — say so on the
      // card instead of leaving "0 chapters" looking like the final answer.
      let chapMeta;
      if (_analyzing.has(b.id)) chapMeta = ' · <span class="tb-working"><span class="spinner"></span> finding chapters…</span>';
      else if (ch) chapMeta = ` · ${ch} chapter${ch === 1 ? '' : 's'}`;
      else if (_analyzeFailed.has(b.id)) chapMeta = ` · <span class="tb-warn" role="button" onclick="event.stopPropagation();detectChaptersFor(${b.id})">no chapters — retry</span>`;
      else chapMeta = '';
      return `<div class="tb-card">
        <div class="tb-cover" onclick="openReader(${b.id})">${cover}${badge}</div>
        <div class="tb-card-body">
          <div class="tb-card-title">${esc(b.title)}</div>
          <div class="tb-card-meta">${b.num_pages} pages${chapMeta}</div>
          <div class="tb-bar"><span style="width:${pct}%"></span></div>
          <div class="tb-card-actions">
            <button class="tb-read-btn" onclick="openReader(${b.id})">${pct > 0 ? 'Resume' : 'Read'}</button>
            <button class="tb-mini" onclick="event.stopPropagation();openChapters(${b.id})">Chapters</button>
            <button class="tb-mini" onclick="renameBook(${b.id})">Rename</button>
            <button class="tb-mini danger" onclick="deleteBook(${b.id})">Delete</button>
          </div>
        </div>
      </div>`;
    }).join('');
    wrap.innerHTML = `<div class="tb-grid">${cards}${uploadCard}</div>`;
  }

  // ── Upload ────────────────────────────────────────────────────────────────
  function pickFile() { document.getElementById('tb-file').click(); }
  document.getElementById('tb-file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    if (file.size > 25 * 1024 * 1024) { alert('That PDF is larger than 25 MB.'); return; }
    const defaultTitle = file.name.replace(/\.pdf$/i, '');
    openModal(`<h2>Upload textbook</h2>
      <p class="tb-sub">${esc(file.name)} · ${(file.size / 1048576).toFixed(1)} MB</p>
      <label class="tb-field"><span>Name this textbook</span>
        <input class="tb-input" id="up-title" maxlength="200" value="${esc(defaultTitle)}"></label>
      <div id="up-status"></div>
      <div class="tb-row-actions">
        <button class="tb-btn ghost" id="up-cancel" onclick="closeModal()">Cancel</button>
        <button class="tb-btn" id="up-go">Upload & parse</button>
      </div>`);
    document.getElementById('up-go').onclick = () => doUpload(file);
  });

  // Upload goes through XHR, not fetch, purely for upload.onprogress: a 20 MB
  // PDF over a phone connection is most of the wait, and a bare spinner for it
  // looks like nothing is happening.
  function _uploadWithProgress(url, fd, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) onProgress(e.loaded / e.total);
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (err) {}
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.detail || `Upload failed (${xhr.status}).`));
      };
      xhr.onerror = () => reject(new Error('Network error during upload.'));
      xhr.onabort = () => reject(new Error('Upload cancelled.'));
      xhr.send(fd);
    });
  }

  function _upStatus(html) {
    const el = document.getElementById('up-status');
    if (el) el.innerHTML = html;
  }

  async function doUpload(file) {
    if (_busy) return;
    const title = (document.getElementById('up-title').value || '').trim();
    if (!title) { _upStatus('<div class="tb-error">Give the textbook a name.</div>'); return; }
    _busy = true;
    document.getElementById('up-go').disabled = true;
    const cancel = document.getElementById('up-cancel'); if (cancel) cancel.disabled = true;
    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('title', title);
      _upStatus('<div class="tb-progress-note"><span class="spinner"></span> Uploading… 0%</div>');
      const data = await _uploadWithProgress(
        `/api/courses/${_courseId}/textbooks`, fd,
        (frac) => {
          const pct = Math.round(frac * 100);
          _upStatus(pct >= 100
            ? '<div class="tb-progress-note"><span class="spinner"></span> Reading the PDF — extracting text, page structure and images…</div>'
            : `<div class="tb-progress-note"><span class="spinner"></span> Uploading… ${pct}%</div>`);
        });
      closeModal();
      await loadBooks();
      // Chapter detection is a separate request; show it running on the card
      // rather than firing it into the void.
      detectChaptersFor(data.id);
    } catch (e) {
      _upStatus(`<div class="tb-error">${esc(e.message || 'Upload failed.')}</div>`);
      document.getElementById('up-go').disabled = false;
      if (cancel) cancel.disabled = false;
    } finally { _busy = false; }
  }

  // ── Background chapter detection with visible state on the book card ────────
  const _analyzing = new Set();      // book ids currently being analyzed
  const _analyzeFailed = new Set();  // book ids whose detection errored

  async function detectChaptersFor(id) {
    if (!id || _analyzing.has(id)) return;
    _analyzing.add(id); _analyzeFailed.delete(id);
    renderLibrary();
    try {
      const r = await fetch(`/api/textbooks/${id}/analyze`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: '{}' });
      if (!r.ok) throw new Error('analyze failed');
      const data = await r.json();
      const b = _books.find(x => x.id === id);
      if (b) b.chapters = data.chapters || [];
      if (!(data.chapters || []).length) _analyzeFailed.add(id);
    } catch (e) {
      _analyzeFailed.add(id);
    } finally {
      _analyzing.delete(id);
      renderLibrary();
    }
  }

  async function renameBook(id) {
    const b = _books.find(x => x.id === id); if (!b) return;
    const name = prompt('Rename textbook', b.title);
    if (name == null) return;
    const t = name.trim(); if (!t) return;
    await fetch(`/api/textbooks/${id}`, { method: 'PATCH',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: t }) }).catch(() => {});
    await loadBooks();
  }

  async function deleteBook(id) {
    const b = _books.find(x => x.id === id); if (!b) return;
    if (!confirm(`Delete “${b.title}”? This removes the book and its page images (any lessons already built are kept).`)) return;
    await fetch(`/api/textbooks/${id}`, { method: 'DELETE' }).catch(() => {});
    await loadBooks();
  }

  // ── Reader ────────────────────────────────────────────────────────────────
  let _rb = null;          // current book (from /reader)
  let _page = 1;           // 1-based
  let _bookmarks = new Set();
  let _saveTimer = null;

  async function openReader(id) {
    // Open the shell first: /reader carries every page's text, so on a big book
    // the tap would otherwise do nothing visible for a second or more.
    const book = _books.find(b => b.id === id);
    document.getElementById('tbr-title').textContent = book ? book.title : 'Opening…';
    document.getElementById('tbr-imgwrap').innerHTML = '<div class="spinner tbr-spin"></div>';
    document.getElementById('tbr-imgwrap').dataset.page = '';
    document.getElementById('tbr-text').innerHTML = '';
    document.getElementById('tbr-pageno').textContent = 'Opening…';
    document.body.classList.add('hide-tabbar');
    document.getElementById('reader').classList.add('open');
    let r;
    try {
      r = await fetch(`/api/textbooks/${id}/reader`);
    } catch (e) { r = null; }
    if (!r || !r.ok) {
      document.getElementById('tbr-imgwrap').innerHTML =
        '<div class="tb-error" style="margin:auto">Could not open this book. Close and try again.</div>';
      document.getElementById('tbr-pageno').textContent = '';
      return;
    }
    _rb = await r.json();
    _splits = [];              // drop the previous book's markers (loadSplits fills)
    _bookmarks = new Set(_rb.bookmarks || []);
    _page = Math.min(Math.max(1, _rb.last_page || 1), _rb.num_pages || 1);
    document.getElementById('tbr-title').textContent = _rb.title;
    // Say it once, up front, when the PDF can't rasterize its whole page range —
    // otherwise it just looks like the book breaks partway through.
    const rp = _rb.renderable_pages;
    const note = document.getElementById('tbr-note');
    if (_rb.has_pages && rp === 0) {
      note.textContent = '⚠️ This PDF can’t be converted to page images, so it reads as extracted text. Everything the AI uses is still here.';
      note.style.display = '';
    } else if (_rb.has_pages && rp && rp < _rb.num_pages) {
      note.textContent = `⚠️ This PDF only renders ${rp} of its ${_rb.num_pages} pages — pages ${rp + 1}+ show their extracted text instead.`;
      note.style.display = '';
    } else {
      note.textContent = ''; note.style.display = 'none';
    }
    renderPage();
    loadSplits();     // non-blocking: markers appear a moment after the page
    loadUnits();      // which chapters already have lessons (for the actions bar)
  }

  function closeReader() {
    if (_seOn) cancelSplitEdit();
    document.getElementById('reader').classList.remove('open');
    document.body.classList.remove('hide-tabbar');
    saveReading(true);
    _rb = null;
    loadBooks();   // refresh progress bars
  }

  function renderPage() {
    if (!_rb) return;
    const n = _rb.num_pages;
    document.getElementById('tbr-pageno').textContent = `Page ${_page} / ${n}`;
    document.getElementById('tbr-prev').disabled = _page <= 1;
    document.getElementById('tbr-next').disabled = _page >= n;
    // Bookmark state
    const bm = document.getElementById('tbr-bookmark');
    const marked = _bookmarks.has(_page);
    bm.textContent = marked ? '★' : '☆';
    bm.classList.toggle('on', marked);
    // Image or text-only fallback. A page the rasterizer can't reach goes
    // straight to text — requesting an image we know will fail just costs a
    // round trip and shows an error.
    const wrap = document.getElementById('tbr-imgwrap');
    // 0 is meaningful (nothing rasterizes) — don't let `||` turn it into "all
    // pages", which would request an image for every page that can't exist.
    const renderable = _rb.renderable_pages == null
      ? _rb.num_pages : _rb.renderable_pages;
    if (_rb.has_pages && _page <= renderable) {
      wrap.innerHTML = '<div class="spinner tbr-spin"></div>';
      const img = new Image();
      img.alt = `Page ${_page}`;
      img.onload = () => {
        if (wrap.dataset.page != String(_page)) return;
        // The page goes in a positioned box so any mid-page unit boundary can be
        // drawn over it at its measured height.
        const box = document.createElement('div');
        box.className = 'tbr-imgbox';
        box.appendChild(img);
        for (const el of _splitOverlays(_page)) box.appendChild(el);
        if (_seOn) box.appendChild(_seOverlay());
        wrap.replaceChildren(box);
      };
      img.onerror = () => showPageImageError(_page);
      wrap.dataset.page = String(_page);
      img.src = `/api/textbooks/${_rb.id}/page/${_page}.jpg`;
      // Preload the next page
      if (_page < Math.min(n, renderable)) new Image().src = `/api/textbooks/${_rb.id}/page/${_page + 1}.jpg`;
    } else {
      wrap.innerHTML = `<div class="tbr-textpane-inner" style="max-width:640px">${_pageTextHtml(_page) || '<span class="tbr-text-empty">No text on this page.</span>'}</div>`;
    }
    // Side/drawer text
    document.getElementById('tbr-text').innerHTML = _pageTextHtml(_page) || '<span class="tbr-text-empty">No extracted text for this page.</span>';
    updateChapterCaption();
    if (_seOn) _seUpdateBar();
    scheduleSave();
  }

  // Redraw ONLY the split overlay + text marker. Re-running renderPage on every
  // drag would rebuild the <img> and flicker the page underneath the rule.
  function _seRefresh() {
    const box = document.querySelector('#tbr-imgwrap .tbr-imgbox');
    if (box) {
      const old = box.querySelector('.tbr-splitedit');
      if (old) old.remove();
      if (_seOn) box.appendChild(_seOverlay());
    } else {
      renderPage();        // text-only page (no image to preserve)
      return;
    }
    document.getElementById('tbr-text').innerHTML =
      _pageTextHtml(_page) || '<span class="tbr-text-empty">No extracted text for this page.</span>';
    _seUpdateBar();
  }

  // ── Mid-page unit boundaries, made visible for validation ──────────────────
  // Splits are stored as one verbatim line of a shared page, which tells us
  // nothing about where that is on the printed page — so the server locates the
  // line in the PDF's text layer (`/splits` → y as a 0–1 fraction) and we draw a
  // dashed rule there. The same boundary is marked in the extracted text, which
  // is the copy the generator actually trims, so both views can be cross-checked.
  let _splits = [];

  async function loadSplits() {
    if (!_rb) return;
    const id = _rb.id;
    // Don't clear `_splits` up front: a save sets optimistic marks and then calls
    // this to reconcile, and wiping first would drop them from state (a re-render
    // mid-fetch — e.g. a resize — would blink the boundary out). Fresh-open
    // clears `_splits` in openReader instead. Replace only once data arrives.
    try {
      const r = await fetch(`/api/textbooks/${id}/splits`);
      if (!r.ok) return;
      const data = await r.json();
      if (!_rb || _rb.id !== id) return;      // reader moved on
      _splits = data.splits || [];
      renderPage();                           // redraw with the settled markers
    } catch (e) { /* best-effort: the reader works without markers */ }
  }

  // Client mirror of the server's `_split_marks`, for optimistic rendering right
  // after a save (before `/splits` recomputes precise y). Every boundary becomes
  // a mid-page mark (anchored, shared page) or two clean page-edge marks. `y` is
  // reused from the marks we already had (keyed by page+line) so mid-page rules
  // don't jump; `editedY` seeds the one boundary just dragged on this page. All
  // marks carry `pending:true` → a settling pulse the reconcile clears.
  function _marksFromChapters(chapters, editedY) {
    const prevY = new Map();
    for (const s of _splits) if (s.y != null) prevY.set(s.page + '|' + _tbNormLine(s.line || ''), s.y);
    const marks = [];
    for (let i = 0; i < chapters.length - 1; i++) {
      const a = chapters[i], b = chapters[i + 1];
      const aEnd = +a.end, bStart = +b.start;
      const anchor = (a.end_anchor || b.start_anchor || '').trim();
      if (anchor) {
        const page = aEnd;                        // anchored boundaries share a page
        const key = page + '|' + _tbNormLine(anchor);
        marks.push({ page, line: anchor, y: prevY.has(key) ? prevY.get(key) : null,
                     kind: 'mid', edge: '', boundary: i,
                     above: a.title || '', below: b.title || '', pending: true });
      } else if (bStart > aEnd) {
        const base = { line: '', y: null, kind: 'page', boundary: i,
                       above: a.title || '', below: b.title || '', pending: true };
        marks.push({ ...base, page: aEnd, edge: 'bottom' });
        marks.push({ ...base, page: bStart, edge: 'top' });
      }
    }
    if (editedY != null)
      for (const m of marks) if (m.kind === 'mid' && m.page === _page && m.y == null) m.y = editedY;
    return marks;
  }

  function _splitsOn(pageNo) {
    // While editing, both views must show the DRAFT boundary — the text pane is
    // the copy generation actually trims, so it's what validates the drag.
    if (_seOn && _seDraft && pageNo === _page) {
      if (_seBi < 0) return [];
      const above = _seDraft[_seBi].title || '';
      const below = _seDraft[_seBi + 1].title || '';
      // y stays null: the rule on the image is the interactive one drawn by
      // `_seOverlay`, so the static image marker must not double up on it. The
      // text pane keys off `kind`/`edge` to place its divider.
      if (_seKind === 'mid')
        return [{ page: pageNo, line: _seAnchor, y: null, kind: 'mid', above, below }];
      return [{ page: pageNo, line: '', y: null, kind: 'page',
                edge: _seKind === 'top' ? 'top' : 'bottom', above, below }];
    }
    return _splits.filter(s => s.page === pageNo);
  }

  function _splitOverlays(pageNo) {
    // On the page being edited the interactive `_seOverlay` rule IS the boundary;
    // a static image marker would double up on it (the text pane still shows the
    // draft divider via _splitsOn).
    if (_seOn && _seDraft && pageNo === _page) return [];
    const out = [];
    for (const s of _splitsOn(pageNo)) {
      const isEdge = s.kind === 'page';
      if (!isEdge && s.y == null) continue;   // couldn't be located — text view only
      const rule = document.createElement('div');
      rule.className = 'tbr-split' + (isEdge ? ` is-edge edge-${s.edge}` : '')
        + (s.pending ? ' pending' : '');
      rule.style.top = isEdge
        ? (s.edge === 'top' ? '0%' : '100%')
        : Math.min(99.5, Math.max(0.5, s.y * 100)) + '%';
      const tag = document.createElement('button');
      tag.type = 'button';
      tag.className = 'tbr-split-tag';
      // The full "X ends · Y begins" wording lives in the caption and the text
      // view; the tag stays short so it never hides the page's own words.
      tag.textContent = isEdge
        ? (s.edge === 'top' ? `✂︎ ${s.below || 'next unit'} starts`
                            : `✂︎ ${s.above || 'this unit'} ends`)
        : '✂︎ split';
      tag.title = 'Tap to manage this unit boundary';
      if (s.boundary != null && !(_seOn && !isEdge)) {
        tag.onclick = (e) => { e.stopPropagation(); openBoundaryMenu(s); };
      } else {
        tag.style.pointerEvents = 'none';
      }
      rule.appendChild(tag);
      out.push(rule);
    }
    return out;
  }

  // ── Deleting a boundary = merging the two units it divides ─────────────────
  // Every division (mid-page or clean page break) is tappable on the page, so
  // structure can be corrected where it's visible instead of in a list of page
  // numbers. Merging renames the survivor with AI, because each old title only
  // described half of what the merged unit now covers.
  function openBoundaryMenu(mark) {
    const above = mark.above || 'the unit above';
    const below = mark.below || 'the unit below';
    const where = mark.kind === 'page'
      ? `between page ${mark.edge === 'top' ? mark.page - 1 : mark.page} and page ${mark.edge === 'top' ? mark.page : mark.page + 1}`
      : `partway down page ${mark.page}`;
    openModal(`<h2>Unit boundary</h2>
      <p class="tb-sub"><b>${esc(above)}</b> ends and <b>${esc(below)}</b> begins ${esc(where)}.
        ${mark.line ? `The cut is at “${esc(mark.line.slice(0, 80))}”.` : ''}</p>
      <div id="bm-status"></div>
      <div class="tb-row-actions" style="flex-wrap:wrap">
        <button class="tb-btn ghost" onclick="closeModal()">Keep it</button>
        <button class="tb-btn ghost" onclick="closeModal();if(!_seOn)toggleSplitEdit()">Move the split</button>
        <button class="tb-btn" id="bm-merge">Merge into one unit</button>
      </div>`);
    document.getElementById('bm-merge').onclick = () => mergeBoundary(mark.boundary);
  }

  async function mergeBoundary(index) {
    if (_busy || !_rb) return;
    _busy = true;
    const btn = document.getElementById('bm-merge');
    if (btn) btn.disabled = true;
    const st = document.getElementById('bm-status');
    if (st) st.innerHTML = '<div class="tb-progress-note"><span class="spinner"></span> Merging the two units and naming the result…</div>';
    try {
      const r = await fetch(`/api/textbooks/${_rb.id}/chapters/merge`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ index }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error((data.detail && data.detail.message) || data.detail || 'Could not merge these units.');
      _applySavedChapters(data.chapters || []);
      closeModal();
      await Promise.all([loadSplits(), loadUnits()]);
      renderPage();
    } catch (e) {
      if (st) st.innerHTML = `<div class="tb-error">${esc(e.message || 'Could not merge these units.')}</div>`;
      if (btn) btn.disabled = false;
    } finally { _busy = false; }
  }

  // Push a freshly-saved chapter list into every view that holds a copy.
  function _applySavedChapters(chapters) {
    if (_rb) _rb.chapters = chapters;
    const lib = _rb && _books.find(x => x.id === _rb.id);
    if (lib) lib.chapters = chapters;
    if (_chBook && _rb && _chBook.id === _rb.id)
      _chBook.chapters = chapters.map(c => ({ ...c }));
    updateChapterCaption();
    renderLibrary();
  }

  // ── Visual split editing: drag the boundary over the rendered page ─────────
  //
  // A split is stored as one VERBATIM LINE of the shared page (that's what
  // `textbook.split_page` trims on), never a coordinate — so a drag has to snap
  // to a real line. `/page/{n}/lines` returns every line with the height its
  // rule would sit at, and dragging picks the nearest one. What you drag is
  // exactly what generation cuts on.
  let _seOn = false;          // split-edit mode active
  let _seLines = null;        // [{text, y}] for the page being edited (null = loading)
  let _seReason = '';         // why there are no lines, if so
  let _seDraft = null;        // working copy of the chapter list
  let _seAnchor = null;       // the mid-page line chosen (null unless kind==='mid')
  let _seBi = -1;             // boundary index being edited (chapters[_seBi] / _seBi+1); -1 = none on this page
  let _seKind = 'none';       // 'mid' | 'top' | 'bottom' | 'none' — where this boundary sits on the current page
  let _seInserted = -1;       // draft index of a chapter this session created (undo on remove)
  let _seBusy = false;
  let _seDragging = false;
  let _seTitleTouched = false;   // don't overwrite a name the learner typed

  function toggleSplitEdit() {
    if (_seOn) { cancelSplitEdit(); return; }
    if (!_rb) return;
    _seOn = true;
    _seLines = null; _seReason = ''; _seInserted = -1; _seBusy = false;
    _seTitleTouched = false;
    _seDraft = (_rb.chapters || []).map(c => ({ ...c }));
    _seRetarget();
    document.getElementById('tbr-splitedit').classList.add('on');
    document.getElementById('tbr-splitbar').style.display = '';
    renderPage();
    loadSplitLines();
  }

  function cancelSplitEdit() {
    _seOn = false; _seDraft = null; _seLines = null; _seAnchor = null;
    _seBi = -1; _seKind = 'none'; _seInserted = -1;
    document.getElementById('tbr-splitedit').classList.remove('on');
    document.getElementById('tbr-splitbar').style.display = 'none';
    renderPage();
  }

  // Which boundary the CURRENT page can edit, and where it sits on that page.
  // Mid-page and clean page-break boundaries are the same object seen at
  // different heights — the whole point of the drag is to slide between them —
  // so a page adjacent to a boundary is editable whether the cut is on the page
  // (mid) or at a clean break just above (top) / below (bottom) it.
  function _seEditBoundary() {
    if (!_seDraft) return { i: -1, kind: 'none' };
    const P = _page;
    for (let i = 0; i < _seDraft.length - 1; i++)
      if (_seDraft[i].end === P && _seDraft[i + 1].start === P) return { i, kind: 'mid' };
    for (let i = 0; i < _seDraft.length - 1; i++)
      if (_seDraft[i].end === P && _seDraft[i + 1].start === P + 1) return { i, kind: 'bottom' };
    for (let i = 0; i < _seDraft.length - 1; i++)
      if (_seDraft[i + 1].start === P && _seDraft[i].end === P - 1) return { i, kind: 'top' };
    return { i: -1, kind: 'none' };
  }

  // Re-point the editor at the boundary adjacent to the current page (run on
  // enter + every page turn). During a drag the target stays fixed.
  function _seRetarget() {
    const t = _seEditBoundary();
    _seBi = t.i;
    _seKind = t.kind;
    _seAnchor = (_seKind === 'mid' && _seBi >= 0)
      ? (_seDraft[_seBi].end_anchor || _seDraft[_seBi + 1].start_anchor || null)
      : null;
  }

  async function loadSplitLines() {
    const id = _rb.id, page = _page;
    try {
      const r = await fetch(`/api/textbooks/${id}/page/${page}/lines`);
      const data = await r.json().catch(() => ({}));
      if (!_seOn || !_rb || _rb.id !== id || _page !== page) return;
      _seLines = data.lines || [];
      _seReason = data.reason || '';
    } catch (e) {
      _seLines = []; _seReason = 'unreadable';
    }
    _seRefresh();
  }

  // The chapter this page reads as, for splitting a page that isn't yet a boundary.
  function _seChapterAt(page) {
    if (!_seDraft) return -1;
    for (let i = 0; i < _seDraft.length; i++) {
      if (page >= _seDraft[i].start && page <= _seDraft[i].end) return i;
    }
    return -1;
  }

  // Where the boundary being edited can land on THIS page: the top edge (a clean
  // break above the page), any text line (a mid-page cut), or the bottom edge (a
  // clean break below it). The edges are only offered when the neighbouring unit
  // keeps at least one page — otherwise a drag there would erase a unit.
  function _seCandidates() {
    const P = _page;
    let head, tail;   // the two page ranges the boundary divides
    if (_seBi >= 0) { head = _seDraft[_seBi]; tail = _seDraft[_seBi + 1]; }
    else { const ci = _seChapterAt(P); if (ci < 0) return []; head = tail = _seDraft[ci]; }
    const cands = [];
    if (P - 1 >= head.start) cands.push({ kind: 'top', y: 0 });
    for (const ln of (_seLines || [])) cands.push({ kind: 'mid', y: ln.y, text: ln.text });
    if (P + 1 <= tail.end) cands.push({ kind: 'bottom', y: 1 });
    return cands;
  }

  // Snap a 0–1 drag height to the nearest candidate (line or edge).
  function _seSnap(frac) {
    const cands = _seCandidates();
    if (!cands.length) return null;
    let best = cands[0], dist = Math.abs(cands[0].y - frac);
    for (const c of cands) {
      const d = Math.abs(c.y - frac);
      if (d < dist) { dist = d; best = c; }
    }
    return best;
  }

  // Move (or create) the edited boundary to `target` — a line (mid-page cut) or
  // an edge (clean page break above/below this page), reshaping the draft.
  function _seApplyTarget(target) {
    if (!target) return;
    const P = _page;
    const line = (target.text || '').trim();
    if (_seBi < 0) {
      // No boundary on this page yet — cut the chapter it belongs to in two.
      const ci = _seChapterAt(P);
      if (ci < 0) return;
      const c = _seDraft[ci];
      const origEnd = c.end, origEndAnchor = c.end_anchor || '';
      const tail = { start: P, end: origEnd, end_anchor: origEndAnchor,
                     title: (line || '').slice(0, 80), skip_hint: false, status: '',
                     lesson_enabled: c.lesson_enabled !== false };
      if (target.kind === 'top') {
        c.end = P - 1; c.end_anchor = ''; tail.start = P; tail.start_anchor = '';
      } else if (target.kind === 'bottom') {
        c.end = P; c.end_anchor = ''; tail.start = P + 1; tail.start_anchor = '';
      } else {
        if (!line) return;
        c.end = P; c.end_anchor = line; tail.start = P; tail.start_anchor = line;
      }
      _seDraft.splice(ci + 1, 0, tail);
      _seInserted = ci + 1;
      _seBi = ci;
    } else {
      const a = _seDraft[_seBi], b = _seDraft[_seBi + 1];
      if (target.kind === 'top') {
        a.end = P - 1; b.start = P; a.end_anchor = ''; b.start_anchor = '';
      } else if (target.kind === 'bottom') {
        a.end = P; b.start = P + 1; a.end_anchor = ''; b.start_anchor = '';
      } else {
        if (!line) return;
        a.end = P; b.start = P; a.end_anchor = line; b.start_anchor = line;
        if (_seInserted === _seBi + 1 && !_seTitleTouched) b.title = line.slice(0, 80);
      }
    }
    _seKind = target.kind;
    _seAnchor = target.kind === 'mid' ? line : null;
    _seRefresh();
  }

  // ✕ on the rule: undo a split created THIS session (give the pages back to one
  // chapter). A pre-existing boundary isn't removed here — that would merge two
  // real units, which is the boundary menu's job; moving it to a clean break is a
  // drag to the edge instead.
  function removeSplitHere() {
    if (_seBi < 0 || _seInserted !== _seBi + 1) return;
    const a = _seDraft[_seBi], b = _seDraft[_seBi + 1];
    a.end = b.end;
    a.end_anchor = b.end_anchor || '';
    _seDraft.splice(_seBi + 1, 1);
    _seInserted = -1;
    _seRetarget();
    _seRefresh();
  }

  async function saveSplitEdit() {
    if (_seBusy || !_seDraft) return;
    _seBusy = true;
    const bar = document.getElementById('tbr-splitbar-msg');
    const save = document.getElementById('tbr-sb-save');
    if (save) save.disabled = true;
    if (_seInserted >= 0 && _seDraft[_seInserted] && !(_seDraft[_seInserted].title || '').trim()) {
      _seDraft[_seInserted].title = (_seAnchor || '').slice(0, 80) || 'Untitled unit';
    }
    const chapters = _seDraft
      .map(c => ({ title: (c.title || '').trim(), start: c.start, end: c.end,
                   skip_hint: !!c.skip_hint, status: c.status || '',
                   lesson_enabled: c.lesson_enabled !== false,
                   start_anchor: c.start_anchor || '', end_anchor: c.end_anchor || '' }))
      .filter(c => c.title && c.start && c.end);
    try {
      const r = await fetch(`/api/textbooks/${_rb.id}/chapters`, { method: 'PUT',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chapters }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Could not save the split.');
      _rb.chapters = data.chapters || [];
      const lib = _books.find(x => x.id === _rb.id); if (lib) lib.chapters = _rb.chapters;
      if (_chBook && _chBook.id === _rb.id) _chBook.chapters = _rb.chapters.map(c => ({ ...c }));
      // The PUT already persisted the split — the only thing `/splits` adds is the
      // precise y for a mid-page anchor (located from the PDF text layer). So show
      // the moved boundary IMMEDIATELY from the saved chapters (reusing the y we
      // had while dragging), marked "pending" with a gentle pulse, instead of
      // blinking it out for a network round-trip. Then reconcile in the
      // background: the pulse clears and y settles to the server's exact value
      // (same positioning code, so no visible jump).
      const editedY = _seKind === 'mid' ? _seCurrentY() : null;
      _splits = _marksFromChapters(_rb.chapters, editedY);
      cancelSplitEdit();           // renders the optimistic, pulsing marks
      updateChapterCaption();
      renderLibrary();
      loadSplits().then(() => renderPage());   // reconcile precise y, clear pulse
    } catch (e) {
      if (bar) bar.innerHTML = `<span class="tb-error">${esc(e.message || 'Could not save.')}</span>`;
      if (save) save.disabled = false;
    } finally { _seBusy = false; }
  }

  function _seUpdateBar() {
    const msg = document.getElementById('tbr-splitbar-msg');
    const rm = document.getElementById('tbr-sb-remove');
    const save = document.getElementById('tbr-sb-save');
    if (!msg) return;
    const hasImg = !!document.querySelector('#tbr-imgwrap .tbr-imgbox');
    const above = _seBi >= 0 ? (_seDraft[_seBi].title || 'this unit') : 'this unit';
    const below = _seBi >= 0 ? (_seDraft[_seBi + 1].title || '') : '';
    if (!hasImg) {
      msg.textContent = 'This page has no image to drag over. Use Chapters → Edit divisions to set the split by line.';
    } else if (_seLines === null) {
      msg.textContent = 'Reading this page…';
    } else if (_seBi >= 0) {
      // An existing boundary touches this page — describe where it sits and how
      // to move it (onto the page, or off to a clean break above/below).
      if (_seKind === 'mid') {
        const line = esc((_seAnchor || '').slice(0, 44));
        const named = below && _tbNormLine(below) !== _tbNormLine(_seAnchor || '');
        msg.innerHTML = `Drag the rule up or down. <b>${esc(above)}</b> ends above it; ` +
          (named ? `<b>${esc(below)}</b> starts` : 'the next unit starts') + ` at “${line}”.`;
      } else if (_seKind === 'top') {
        msg.innerHTML = `Clean break above this page — <b>${esc(above)}</b> ends on the previous page, ` +
          `<b>${esc(below) || 'the next unit'}</b> starts here. Drag down onto the page to cut mid-page.`;
      } else {
        msg.innerHTML = `Clean break below this page — <b>${esc(above)}</b> ends here, ` +
          `<b>${esc(below) || 'the next unit'}</b> starts on the next page. Drag up onto the page to cut mid-page.`;
      }
    } else if (!_seLines.length) {
      msg.textContent = _seReason === 'no_pdf'
        ? 'This book has no stored PDF, so a split can’t be placed visually. Use Chapters → Edit divisions.'
        : 'This page has no text layer to split on (a scan, or a page re-read by AI). Use Chapters → Edit divisions.';
    } else {
      msg.textContent = 'Tap the page where the next unit begins — a line for a mid-page cut, or the very top/bottom for a clean page break.';
    }
    // ✕ only undoes a split created this session; an existing boundary moves to a
    // clean break by dragging to the edge, and merges via the boundary menu.
    if (rm) rm.style.display = (_seInserted >= 0 && _seInserted === _seBi + 1) ? '' : 'none';
    if (save) save.disabled = false;
    // A split that CREATED a unit needs a name — the anchor line is only a
    // guess (right when it's a heading, noise when it isn't), and an unnamed
    // unit would show up in the table of contents as that raw line.
    const nameWrap = document.getElementById('tbr-splitbar-name');
    const nameInp = document.getElementById('tbr-sb-title');
    const naming = _seInserted >= 0 && !!_seDraft && !!_seDraft[_seInserted];
    if (nameWrap) nameWrap.style.display = naming ? '' : 'none';
    if (naming && nameInp && document.activeElement !== nameInp) {
      nameInp.value = _seDraft[_seInserted].title || '';
    }
  }

  function renameSplitUnit(value) {
    if (_seInserted < 0 || !_seDraft || !_seDraft[_seInserted]) return;
    _seTitleTouched = true;
    _seDraft[_seInserted].title = value;
  }

  // Overlay for edit mode: a draggable rule, a live ghost of the line under the
  // pointer, and tap-to-place when there's no split yet.
  function _seOverlay() {
    const box = document.createElement('div');
    box.className = 'tbr-splitedit';
    const hint = document.createElement('div');
    hint.className = 'se-hint';
    hint.textContent = _seLines === null ? 'Reading this page…' : '✂︎ Split mode';
    box.appendChild(hint);

    const ghost = document.createElement('div');
    ghost.className = 'tbr-se-ghost';
    ghost.style.display = 'none';
    box.appendChild(ghost);

    const fracOf = (clientY) => {
      const r = box.getBoundingClientRect();
      return r.height ? Math.min(1, Math.max(0, (clientY - r.top) / r.height)) : 0;
    };
    const label = (c) => c.kind === 'top' ? '✂︎ clean break above this page'
      : c.kind === 'bottom' ? '✂︎ clean break below this page'
      : '✂︎ ' + ((c.text || '').slice(0, 46) || '·');
    const preview = (clientY) => {
      const near = _seSnap(fracOf(clientY));
      if (!near) return null;
      ghost.style.display = '';
      ghost.style.top = (near.y * 100) + '%';
      hint.textContent = label(near);
      return near;
    };

    const canPlace = _seCandidates().length > 0;
    if (canPlace) {
      box.addEventListener('pointermove', (e) => { if (!_seDragging) preview(e.clientY); });
      box.addEventListener('pointerleave', () => {
        if (_seDragging) return;
        ghost.style.display = 'none';
        hint.textContent = '✂︎ Split mode';
      });
      // Tap on empty page area places/moves the boundary (the rule handles drags).
      box.addEventListener('pointerdown', (e) => {
        if (e.target !== box && e.target !== ghost && e.target !== hint) return;
        const near = preview(e.clientY);
        if (near) _seApplyTarget(near);
      });
    }

    // An existing boundary on this page gets a draggable rule — whether it sits
    // mid-page or at a clean top/bottom edge. Dragging slides it freely between
    // the two, which is the whole point: an edge split isn't a different thing.
    if (_seBi >= 0) {
      const y = _seCurrentY();
      const rule = document.createElement('div');
      rule.className = 'tbr-se-rule' + (_seKind !== 'mid' ? ' is-edge' : '');
      rule.style.top = (y * 100) + '%';
      const grip = document.createElement('div');
      grip.className = 'tbr-se-grip';
      grip.textContent = '⇕ drag';
      rule.appendChild(grip);
      if (_seInserted === _seBi + 1) {
        const del = document.createElement('button');
        del.className = 'tbr-se-del';
        del.type = 'button';
        del.textContent = '✕';
        del.title = 'Undo this split';
        del.addEventListener('pointerdown', (e) => e.stopPropagation());
        del.addEventListener('click', (e) => { e.stopPropagation(); removeSplitHere(); });
        rule.appendChild(del);
      }
      rule.addEventListener('pointerdown', (e) => {
        if (e.target.classList.contains('tbr-se-del')) return;
        e.preventDefault(); e.stopPropagation();
        _seDragging = true;
        rule.classList.add('dragging');
        rule.setPointerCapture(e.pointerId);
      });
      rule.addEventListener('pointermove', (e) => {
        if (!_seDragging) return;
        const near = preview(e.clientY);
        if (near) rule.style.top = (near.y * 100) + '%';
      });
      const drop = (e) => {
        if (!_seDragging) return;
        _seDragging = false;
        rule.classList.remove('dragging');
        const near = _seSnap(fracOf(e.clientY));
        ghost.style.display = 'none';
        if (near) _seApplyTarget(near); else _seRefresh();
      };
      rule.addEventListener('pointerup', drop);
      rule.addEventListener('pointercancel', drop);
      box.appendChild(rule);
    }
    return box;
  }

  // Where the edited boundary sits on the page: a mid-page anchor's line height,
  // else the top (0) or bottom (1) edge for a clean break.
  function _seCurrentY() {
    if (_seKind === 'top') return 0;
    if (_seKind === 'bottom') return 1;
    if (!_seAnchor) return 0.5;
    const hit = _seLines && _seLines.find(l => _tbNormLine(l.text) === _tbNormLine(_seAnchor));
    if (hit) return hit.y;
    const idx = _seLines ? _tbAnchorIdx(_seLines.map(l => l.text), _seAnchor) : -1;
    if (idx >= 0) return _seLines[idx].y;
    const saved = _splits.find(s => s.page === _page && s.y != null);
    return saved ? saved.y : 0.5;
  }

  // Page text with the split boundary marked: lines above it dimmed (they belong
  // to the previous unit), a labelled dashed rule, then the anchor line itself
  // highlighted.
  function _pageTextHtml(pageNo) {
    const raw = _pageTextRaw(pageNo);
    if (!raw) return '';
    const all = _splitsOn(pageNo);
    const marks = all.filter(m => m.kind !== 'page');
    // A clean page break has no line to sit on — rule it off at the very
    // top/bottom of the page's text, matching the marker on the image.
    const edgeRule = (m) => `<div class="tbr-splitline"><span>✂︎ ${esc(m.above || 'previous unit')} ends · ${esc(m.below || 'next unit')} begins</span></div>`;
    const top = all.filter(m => m.kind === 'page' && m.edge === 'top').map(edgeRule).join('');
    const bottom = all.filter(m => m.kind === 'page' && m.edge === 'bottom').map(edgeRule).join('');
    if (!marks.length) return top + esc(raw) + bottom;
    const lines = raw.split('\n');
    const idxFor = (line) => _tbAnchorIdx(lines, line);
    const cuts = marks.map(m => ({ at: idxFor(m.line), m }))
                      .filter(c => c.at >= 0)
                      .sort((a, b) => a.at - b.at);
    if (!cuts.length) return top + esc(raw) + bottom;
    const out = [];
    let cut = 0;
    lines.forEach((line, i) => {
      if (cut < cuts.length && i === cuts[cut].at) {
        const m = cuts[cut].m;
        out.push(`<div class="tbr-splitline"><span>✂︎ ${esc(m.above || 'previous unit')} ends · ${esc(m.below || 'next unit')} begins</span></div>`);
        out.push(`<span class="tbr-anchorline">${esc(line)}</span>`);
        cut++;
        return;
      }
      // Before the first cut = the earlier unit's half of this page.
      out.push(cut === 0 && cuts.length
        ? `<span class="tbr-above">${esc(line)}</span>` : esc(line));
    });
    return top + out.join('\n') + bottom;
  }

  // An <img> only reports "it failed", never why. Re-request the same URL so the
  // server's actual reason (page beyond what the PDF renders, book uploaded
  // before page images, session expired…) is shown instead of a dead end — and
  // keep the page readable by falling back to its extracted text.
  async function showPageImageError(pageNo) {
    const wrap = document.getElementById('tbr-imgwrap');
    if (wrap.dataset.page != String(pageNo) || pageNo !== _page) return;
    let detail = '';
    try {
      const r = await fetch(`/api/textbooks/${_rb.id}/page/${pageNo}.jpg`);
      if (r.status === 401) detail = 'Your session expired — reload the page to sign back in.';
      else if (!r.ok) detail = ((await r.json().catch(() => ({}))).detail) || '';
    } catch (e) { detail = 'Network error while loading this page image.'; }
    if (wrap.dataset.page != String(pageNo) || pageNo !== _page) return;
    const text = _pageTextHtml(pageNo);
    wrap.innerHTML =
      `<div style="max-width:640px;margin:auto">
         <div class="tb-error">${esc(detail || 'Couldn’t render this page.')}</div>
         ${text ? `<div class="tbr-textpane-inner" style="padding-left:0;padding-right:0">${text}</div>`
                : '<div class="tbr-text-empty">No extracted text for this page either.</div>'}
       </div>`;
  }

  function updateChapterCaption() {
    const cap = document.getElementById('tbr-chapcap');
    if (!cap) return;
    // A mid-page split makes the boundary page belong to BOTH units, so name
    // both rather than silently showing whichever comes first.
    const here = (_rb && _rb.chapters || []).filter(c => _page >= c.start && _page <= c.end);
    if (here.length > 1) {
      cap.textContent = `✂︎ ${here.map(c => c.title).join(' → ')} · both share p${_page}`;
    } else if (here.length === 1) {
      const ch = here[0];
      cap.textContent = `${ch.title} · p${_page} of ${ch.start}–${ch.end}`;
    } else {
      cap.textContent = '';
    }
    cap.style.display = here.length ? '' : 'none';
    // One chapter = one unit, so say when this one is already built rather than
    // offering to make a second copy of it.
    const btn = document.getElementById('tbr-lessons-btn');
    if (btn) {
      const unit = _unitForChapter(_currentChapter() ? _currentChapter().idx : -1);
      btn.textContent = unit ? '📘 Lessons made · regenerate' : '📘 Turn chapter into lessons';
    }
  }

  function _pageText(n) {
    const p = (_rb.pages || []).find(x => x.page === n);
    return p ? esc(p.text) : '';
  }

  function _pageTextRaw(n) {
    const p = (_rb.pages || []).find(x => x.page === n);
    return p ? (p.text || '') : '';
  }

  function turnPage(d) {
    if (!_rb) return;
    const t = _page + d;
    if (t < 1 || t > _rb.num_pages) return;
    _page = t;
    if (_seOn) {
      // Keep editing across pages — the draft is book-wide, so several splits
      // can be placed and saved together. Re-target it at the new page.
      _seLines = null; _seReason = '';
      _seRetarget();
      loadSplitLines();
    }
    renderPage();
  }

  // ── Chapters: TOC + inline editor + AI (re)detect ──────────────────────────
  let _chBook = null;      // { id, title, num_pages, chapters, fromReader }
  let _chEdit = false;
  let _chDraft = null;     // working copy while editing
  let _splitAt = null;     // boundary index i currently editing a mid-page split
  let _splitPageNo = null; // the shared page whose lines we're picking
  let _splitLines = null;  // that page's text split into lines (null = loading)

  function _bookById(id) {
    if (_rb && _rb.id === id) return _rb;
    return _books.find(b => b.id === id) || null;
  }

  function openChapters(id, fromReader) {
    const b = _bookById(id);
    if (!b) return;
    _chBook = { id: b.id, title: b.title, num_pages: b.num_pages,
                chapters: (b.chapters || []).map(c => ({ ...c })), fromReader: !!fromReader };
    _chEdit = false; _chDraft = null;
    renderChapters();
  }

  function startEditChapters() {
    _chDraft = (_chBook.chapters || []).map(c => ({ ...c }));
    if (!_chDraft.length) _chDraft.push({ title: '', start: 1, end: _chBook.num_pages, skip_hint: false, status: '', lesson_enabled: true });
    _chEdit = true;
    _splitAt = null; _splitLines = null;
    renderChapters();
  }

  function _syncDraft() {
    if (!_chDraft) return;
    document.querySelectorAll('.toc-erow input').forEach(inp => {
      const i = +inp.dataset.i, k = inp.dataset.k;
      if (!_chDraft[i]) return;
      _chDraft[i][k] = k === 'title' ? inp.value
        : (k === 'lesson_enabled' ? inp.checked : (parseInt(inp.value, 10) || 0));
    });
  }

  function addChapterRow() {
    _syncDraft();
    const last = _chDraft[_chDraft.length - 1];
    const start = last ? Math.min(_chBook.num_pages, (last.end || 0) + 1) : 1;
    _chDraft.push({ title: '', start, end: start, skip_hint: false, status: '', lesson_enabled: true });
    _splitAt = null;
    renderChapters();
  }

  function deleteChapterRow(i) { _syncDraft(); _chDraft.splice(i, 1); _splitAt = null; renderChapters(); }

  // ── Mid-page split between two adjacent units (mirror of textbook.py) ───────
  function _tbNormLine(s) { return (s || '').replace(/\s+/g, ' ').trim().toLowerCase(); }
  // Mirrors textbook._anchor_line_index: an EXACT line match anywhere beats a
  // partial one, and a partial match needs a substantial run of characters —
  // otherwise a short early line ("Unit") claims a boundary further down.
  const _TB_MIN_PREFIX = 8, _TB_MAX_SPAN = 12;
  function _tbAnchorIdx(lines, anchor) {
    const t = _tbNormLine(anchor);
    if (!t) return -1;
    const norm = lines.map(_tbNormLine);
    for (let i = 0; i < norm.length; i++) if (norm[i] && norm[i] === t) return i;
    // The anchor may be spread over consecutive lines (a PDF that positions each
    // word separately extracts one word per line) — rejoin a short run.
    const squeezed = t.replace(/ /g, '');
    for (let i = 0; i < norm.length; i++) {
      if (!norm[i]) continue;
      let joined = '';
      for (let j = i; j < Math.min(i + _TB_MAX_SPAN, norm.length); j++) {
        if (!norm[j]) break;
        joined = (joined + ' ' + norm[j]).trim();
        if (joined === t || joined.replace(/ /g, '') === squeezed) return i;
        if (joined.length > t.length + 2) break;
      }
    }
    for (let i = 0; i < norm.length; i++) {
      const n = norm[i];
      if (n && Math.min(n.length, t.length) >= _TB_MIN_PREFIX &&
          (n.startsWith(t) || t.startsWith(n))) return i;
    }
    return -1;
  }

  function _boundaryRowHtml(i) {
    const a = _chDraft[i], b = _chDraft[i + 1];
    if (!a || !b) return '';
    // Only meaningful where the two units meet at a page: adjacent (b.start =
    // a.end + 1) or already sharing the boundary page (b.start = a.end).
    if (b.start > a.end + 1 || b.start < a.end) return '';
    const shared = b.start === a.end;
    const hasSplit = shared && !!(a.end_anchor || b.start_anchor);
    const pageNo = a.end;
    const label = hasSplit ? `✂︎ Split set on page ${pageNo} — edit`
                           : `✂︎ Split page ${pageNo} between these units`;
    return `<div class="toc-splitrow">
      <button type="button" class="toc-splitbtn${hasSplit ? ' on' : ''}" onclick="toggleBoundarySplit(${i})">${label}</button>
      ${_splitAt === i ? _splitPickerHtml(i, pageNo) : ''}
    </div>`;
  }

  function _splitPickerHtml(i, pageNo) {
    if (_splitLines === null) {
      return `<div class="split-picker"><div class="tb-progress-note"><span class="spinner"></span> Loading page ${pageNo}…</div></div>`;
    }
    const anchor = _chDraft[i + 1].start_anchor || _chDraft[i].end_anchor || '';
    const activeIdx = _tbAnchorIdx(_splitLines, anchor);
    const rows = _splitLines.map((ln, li) => {
      let cls = 'split-line';
      if (activeIdx >= 0) cls += li < activeIdx ? ' up' : ' down';
      if (activeIdx === li) cls += ' active';
      return `<button type="button" class="${cls}" onclick="setBoundarySplit(${i},${li})">${esc(ln.trim() || '·')}</button>`;
    }).join('') || '<div class="split-hint">This page has no extracted text to split on.</div>';
    return `<div class="split-picker">
      <p class="split-hint">Tap the line where the <b>next</b> unit begins. Lines above stay with “${esc(_chDraft[i].title || 'this unit')}”; that line and below start “${esc(_chDraft[i + 1].title || 'the next unit')}”.</p>
      <div class="split-lines">${rows}</div>
      ${anchor ? `<button type="button" class="split-clear" onclick="clearBoundarySplit(${i})">Remove split (use the whole page in one unit)</button>` : ''}
    </div>`;
  }

  async function toggleBoundarySplit(i) {
    _syncDraft();
    if (_splitAt === i) { _splitAt = null; renderChapters(); return; }
    _splitAt = i; _splitLines = null;
    _splitPageNo = _chDraft[i].end;
    renderChapters();
    try {
      const r = await fetch(`/api/textbooks/${_chBook.id}/pages?start=${_splitPageNo}&end=${_splitPageNo}`);
      const data = await r.json().catch(() => ({}));
      const txt = ((data.pages || [])[0] || {}).text || '';
      _splitLines = txt.split('\n');
    } catch (e) {
      _splitLines = [];
    }
    if (_splitAt === i) renderChapters();
  }

  function setBoundarySplit(i, lineIdx) {
    _syncDraft();
    const line = (_splitLines[lineIdx] || '').replace(/\s+/g, ' ').trim();
    const a = _chDraft[i], b = _chDraft[i + 1];
    a.end = _splitPageNo;          // earlier unit ends on the shared page…
    b.start = _splitPageNo;        // …and the later unit also includes it
    a.end_anchor = line;           // earlier unit trims from the split onward
    b.start_anchor = line;         // later unit trims everything before the split
    renderChapters();
  }

  function clearBoundarySplit(i) {
    _syncDraft();
    const a = _chDraft[i], b = _chDraft[i + 1];
    a.end_anchor = ''; b.start_anchor = '';
    if (b.start === a.end) b.start = Math.min(_chBook.num_pages, a.end + 1);
    renderChapters();
  }

  function renderChapters() {
    const b = _chBook;
    if (!_chEdit) _splitAt = null;
    const curCh = (_rb && _rb.id === b.id) ? _page : null;
    let body;
    if (_chEdit) {
      body = `<div class="toc-list">` + _chDraft.map((c, i) => `
        <div class="toc-erow">
          <input class="tb-input toc-etitle" data-i="${i}" data-k="title" value="${esc(c.title)}" placeholder="Chapter title">
          <input class="toc-numin" data-i="${i}" data-k="start" type="number" min="1" max="${b.num_pages}" value="${c.start}" title="First page">
          <span class="toc-dash">–</span>
          <input class="toc-numin" data-i="${i}" data-k="end" type="number" min="1" max="${b.num_pages}" value="${c.end}" title="Last page">
          <label class="toc-lessons" title="Hide this chapter from the lesson tree and prevent lesson generation">
            <input data-i="${i}" data-k="lesson_enabled" type="checkbox"${c.lesson_enabled !== false ? ' checked' : ''}>
            <span>Use for lessons</span>
          </label>
          <button class="toc-del" onclick="deleteChapterRow(${i})" title="Remove chapter">✕</button>
        </div>` + _boundaryRowHtml(i)).join('') + `</div>
        <button class="toc-add" onclick="addChapterRow()">＋ Add chapter</button>
        <div id="ch-status"></div>
        <div class="tb-row-actions">
          <button class="tb-btn ghost" onclick="_chEdit=false;renderChapters()">Cancel</button>
          <button class="tb-btn" id="ch-save" onclick="saveChapters()">Save chapters</button>
        </div>`;
    } else if (!b.chapters.length) {
      body = `<div class="toc-empty">No chapters yet.<br>Let AI find them from the book's structure, or add them by hand.</div>
        <div id="ch-status"></div>
        <div class="tb-row-actions" style="justify-content:center">
          <button class="tb-btn ghost" onclick="startEditChapters()">＋ Add manually</button>
          <button class="tb-btn" id="ch-redetect" onclick="redetectChapters()">✨ Detect with AI</button>
        </div>`;
    } else {
      body = `<div class="toc-tools">
          <button onclick="startEditChapters()">✏️ Edit divisions</button>
          <button id="ch-redetect" onclick="redetectChapters()">✨ Re-detect with AI</button>
        </div>
        <div id="ch-status"></div>
        <div class="toc-list">` + b.chapters.map(c => {
          const here = curCh != null && curCh >= c.start && curCh <= c.end;
          const tags = [];
          if (c.skip_hint) tags.push('<span class="toc-skip">skip</span>');
          if (c.lesson_enabled === false) tags.push('<span class="toc-readonly">read only</span>');
          if (c.status === 'queued') tags.push('lessons made');
          if (c.start_anchor || c.end_anchor) tags.push('<span class="toc-split">✂︎ split</span>');
          // Spell out where a shared page is cut, so the split can be sanity-
          // checked from the contents list without opening the page.
          const cuts = [];
          if (c.start_anchor) cuts.push(`starts on p${c.start} at “${esc(c.start_anchor)}”`);
          if (c.end_anchor) cuts.push(`ends on p${c.end} before “${esc(c.end_anchor)}”`);
          return `<div class="toc-row${here ? ' here' : ''}" onclick="gotoChapter(${c.start})">
            <div class="toc-row-main">
              <div class="toc-row-title">${esc(c.title)}</div>
              <div class="toc-row-pages">pages ${c.start}–${c.end}${tags.length ? ' · ' + tags.join(' · ') : ''}</div>
              ${cuts.length ? `<div class="toc-row-cut">✂︎ ${cuts.join(' · ')}</div>` : ''}
            </div>
            <span class="toc-go">›</span>
          </div>`;
        }).join('') + `</div>`;
    }
    const sub = _chEdit ? 'Set each chapter’s page range — and split a shared page between two units' : (b.chapters.length ? 'Tap a chapter to read from there' : '');
    openModal(`<h2>Chapters</h2><p class="tb-sub">${esc(b.title)}${sub ? ' · ' + sub : ''}</p>${body}`);
  }

  function _applyChapters(chs) {
    _chBook.chapters = (chs || []).map(c => ({ ...c }));
    const b = _books.find(x => x.id === _chBook.id); if (b) b.chapters = chs || [];
    if (_rb && _rb.id === _chBook.id) { _rb.chapters = chs || []; updateChapterCaption(); }
    renderLibrary();
  }

  async function saveChapters() {
    _syncDraft();
    const chapters = _chDraft
      .map(c => ({ title: (c.title || '').trim(), start: c.start, end: c.end,
                   skip_hint: !!c.skip_hint, status: c.status || '',
                   lesson_enabled: c.lesson_enabled !== false,
                   start_anchor: c.start_anchor || '', end_anchor: c.end_anchor || '' }))
      .filter(c => c.title && c.start && c.end);
    const st = document.getElementById('ch-status');
    const btn = document.getElementById('ch-save'); if (btn) btn.disabled = true;
    try {
      const r = await fetch(`/api/textbooks/${_chBook.id}/chapters`, { method: 'PUT',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chapters }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Could not save chapters.');
      _applyChapters(data.chapters);
      _chEdit = false;
      renderChapters();
    } catch (e) {
      if (st) st.innerHTML = `<div class="tb-error">${esc(e.message || 'Could not save.')}</div>`;
      if (btn) btn.disabled = false;
    }
  }

  async function redetectChapters() {
    const st = document.getElementById('ch-status');
    const btn = document.getElementById('ch-redetect'); if (btn) btn.disabled = true;
    if (st) st.innerHTML = '<div class="tb-progress-note"><span class="spinner"></span> Scanning the book for chapter boundaries, then checking for units that start mid-page…</div>';
    try {
      const r = await fetch(`/api/textbooks/${_chBook.id}/analyze`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: '{}' });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Detection failed.');
      _applyChapters(data.chapters);
      _chEdit = false;
      renderChapters();
      const st2 = document.getElementById('ch-status');
      if (!(data.chapters || []).length && st2) {
        st2.innerHTML = '<div class="tb-error">AI couldn’t find clear chapter breaks. Add them by hand instead.</div>';
      } else if (st2 && data.refined === false) {
        // Detection is saved even when the mid-page pass doesn't finish, so this
        // is a partial success, not a failure to redo from scratch.
        st2.innerHTML = '<p class="split-hint">Chapters saved. Mid-page splits weren’t checked this time — re-run detection, or set a split by hand in “Edit divisions”.</p>';
      }
    } catch (e) {
      // The first pass is persisted server-side before refinement runs, so a
      // timeout here may still have produced chapters. Show whatever landed.
      const saved = await _refetchChapters();
      if (saved && saved.length) {
        _applyChapters(saved);
        _chEdit = false;
        renderChapters();
        const st2 = document.getElementById('ch-status');
        if (st2) st2.innerHTML = `<p class="split-hint">${esc(e.message || 'Detection was interrupted')} — but the chapters it had already found are saved below.</p>`;
        return;
      }
      if (st) st.innerHTML = `<div class="tb-error">${esc(e.message || 'Detection failed.')} Try again.</div>`;
      if (btn) btn.disabled = false;
    }
  }

  async function _refetchChapters() {
    try {
      const r = await fetch(`/api/courses/${_courseId}/textbooks`);
      const data = await r.json();
      _books = data.textbooks || [];
      const b = _books.find(x => x.id === _chBook.id);
      return b ? (b.chapters || []) : null;
    } catch (e) { return null; }
  }

  function gotoChapter(p) {
    const id = _chBook.id;
    const page = Math.min(Math.max(1, p), _chBook.num_pages);
    closeModal();
    if (_rb && _rb.id === id && document.getElementById('reader').classList.contains('open')) {
      _page = page; renderPage();
    } else {
      openReader(id).then(() => { _page = page; renderPage(); });
    }
  }

  function toggleTextPane() {
    const el = document.getElementById('reader');
    const on = el.classList.toggle('show-text');
    document.getElementById('tbr-texttoggle').classList.toggle('on', on);
  }

  function toggleBookmark() {
    if (!_rb) return;
    if (_bookmarks.has(_page)) _bookmarks.delete(_page); else _bookmarks.add(_page);
    renderPage();
    saveReading();
  }

  function scheduleSave() {
    clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => saveReading(), 800);
  }
  async function saveReading(now) {
    if (!_rb) return;
    const body = JSON.stringify({ last_page: _page, bookmarks: [..._bookmarks] });
    try {
      if (now && navigator.sendBeacon) {
        navigator.sendBeacon(`/api/textbooks/${_rb.id}/reading`, new Blob([body], { type: 'application/json' }));
      } else {
        await fetch(`/api/textbooks/${_rb.id}/reading`, { method: 'PUT',
          headers: { 'Content-Type': 'application/json' }, body });
      }
    } catch (e) { /* best-effort */ }
  }

  // Keyboard nav (desktop)
  document.addEventListener('keydown', (e) => {
    if (document.getElementById('tb-modal').classList.contains('open')) {
      if (e.key === 'Escape') closeModal();
      return;
    }
    if (!document.getElementById('reader').classList.contains('open')) return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
    if (e.key === 'ArrowLeft') turnPage(-1);
    else if (e.key === 'ArrowRight') turnPage(1);
    else if (e.key === 'Escape') closeReader();
  });

  // ── Which chapter contains the current page (for reader actions) ───────────
  function _currentChapter() {
    if (!_rb) return null;
    const idx = (_rb.chapters || []).findIndex(c => _page >= c.start && _page <= c.end);
    if (idx >= 0) {
      const ch = _rb.chapters[idx];
      return { start: ch.start, end: ch.end, title: ch.title, idx };
    }
    return { start: _page, end: _page, title: `Page ${_page}`, idx: -1 };
  }

  // ── Which chapters already became units ───────────────────────────────────
  // One chapter = one unit. Knowing which chapters are already built is what
  // turns a second "Turn into lessons" tap into "Regenerate" instead of a
  // duplicate copy of the same chapter sitting in the roadmap.
  let _rbUnits = [];

  async function loadUnits() {
    _rbUnits = [];
    if (!_rb) return;
    const id = _rb.id;
    try {
      const r = await fetch(`/api/textbooks/${id}/units`);
      if (!r.ok) return;
      const data = await r.json();
      if (!_rb || _rb.id !== id) return;
      _rbUnits = data.units || [];
      updateChapterCaption();     // the actions bar reads this
    } catch (e) { /* best-effort */ }
  }

  function _unitForChapter(idx) {
    return idx < 0 ? null : _rbUnits.find(u => u.chapter_idx === idx) || null;
  }

  // ── Turn chapter into lessons (creates a textbook unit) ────────────────────
  async function readerCreateLessons() {
    const ch = _currentChapter();
    const unit = _unitForChapter(ch.idx);
    const total = unit ? unit.lesson_count + unit.queued_remaining : 0;
    if (unit) {
      openModal(`<h2>Already made 📘</h2>
        <p class="tb-sub"><b>${esc(unit.title)}</b> already has ${total} lesson${total === 1 ? '' : 's'} from this chapter${unit.done_count ? ` (${unit.done_count} finished)` : ''}.
          Regenerating deletes them and plans the chapter again from scratch.</p>
        <div id="cl-status"></div>
        <div class="tb-row-actions" style="flex-wrap:wrap">
          <button class="tb-btn ghost" onclick="closeModal()">Keep reading</button>
          <a class="tb-btn ghost" href="/learn" style="text-decoration:none">Go to Learn →</a>
          <button class="tb-btn" id="cl-go">Regenerate lessons</button>
        </div>`);
    } else {
      openModal(`<h2>Turn into lessons</h2>
        <p class="tb-sub">Create a multi-lesson unit from <b>${esc(ch.title)}</b> (pages ${ch.start}–${ch.end}). It appears in Learn under “From your textbooks”.</p>
        <div id="cl-status"></div>
        <div class="tb-row-actions">
          <button class="tb-btn ghost" onclick="closeModal()">Cancel</button>
          <button class="tb-btn" id="cl-go">Create unit</button>
        </div>`);
    }
    document.getElementById('cl-go').onclick = async () => {
      if (_busy) return;
      if (unit && !confirm(`Delete ${total} lesson${total === 1 ? '' : 's'} and rebuild “${unit.title}” from the book?`)) return;
      _busy = true;
      document.getElementById('cl-go').disabled = true;
      document.getElementById('cl-status').innerHTML = '<div class="tb-progress-note"><span class="spinner"></span> Planning the unit and writing the first lesson… this can take a moment.</div>';
      try {
        const r = await fetch(`/api/textbooks/${_rb.id}/unit`, { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ start: ch.start, end: ch.end, section_title: ch.title,
                                 chapter_idx: ch.idx, replace: !!unit }) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error((data.detail && data.detail.message) || data.detail || 'Could not create the unit.');
        const up = data.unit_plan || {};
        await loadUnits();
        openModal(`<h2>Unit ${unit ? 'rebuilt' : 'created'} 📘</h2>
          <p class="tb-sub">“${esc(up.title || ch.title)}” — ${up.lesson_count || 1} lesson${up.lesson_count === 1 ? '' : 's'}.
            The first is ready and the next is already building; Learn can finish the rest in one go.</p>
          <div class="tb-row-actions">
            <button class="tb-btn ghost" onclick="closeModal()">Keep reading</button>
            <a class="tb-btn" href="/learn" style="text-decoration:none">Go to Learn →</a>
          </div>`);
      } catch (e) {
        document.getElementById('cl-status').innerHTML = `<div class="tb-error">${esc(e.message || 'Could not create the unit.')}</div>`;
        const go = document.getElementById('cl-go'); if (go) go.disabled = false;
      } finally { _busy = false; }
    };
  }

  // ── Build vocab deck from the current chapter ──────────────────────────────
  let _vItems = [], _vMeta = null, _vName = '';
  async function readerBuildVocab() {
    const ch = _currentChapter();
    _vItems = []; _vMeta = null;
    openModal(`<div class="tb-vloading"><span class="spinner"></span>
      <h3>Reading pages ${ch.start}–${ch.end} for vocabulary…</h3>
      <p>Pulling every word out of the chapter and checking it against your deck. Longer chapters take a little while.</p></div>`);
    try {
      const r = await fetch(`/api/textbooks/${_rb.id}/vocab`, { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ start: ch.start, end: ch.end }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Could not extract vocabulary.');
      _vItems = (data.items || []).map(it => ({ ...it, _pick: !it.in_deck }));
      _vMeta = { section: data.section, lang: data.lang, start: data.start, end: data.end };
      _vName = data.section || ch.title || `Pages ${ch.start}–${ch.end}`;
      renderVocabReview();
    } catch (e) {
      openModal(`<div class="tb-vloading"><div style="font-size:2rem">🔍</div>
        <h3>No vocabulary found</h3><p>${esc(e.message || "We couldn't pull words from these pages. Try a different chapter, or re-read the pages with AI from the review screen.")}</p>
        <div class="tb-row-actions" style="justify-content:center;margin-top:12px"><button class="tb-btn ghost" onclick="closeModal()">Close</button></div></div>`);
    }
  }

  function renderVocabReview() {
    if (!_vItems.length) {
      document.getElementById('tb-sheet').innerHTML = `<div class="tb-vloading"><div style="font-size:2rem">🔍</div>
        <h3>No vocabulary found</h3><p>These pages had no extractable vocabulary.</p>
        <div class="tb-row-actions" style="justify-content:center;margin-top:12px"><button class="tb-btn ghost" onclick="closeModal()">Close</button></div></div>`;
      return;
    }
    const inDeck = _vItems.filter(it => it.in_deck).length;
    const total = _vItems.length, newC = total - inDeck;
    const sel = _vItems.filter(it => it._pick).length;
    const rows = _vItems.map((it, i) => `<label class="tb-vrow${it.in_deck ? ' in' : ''}">
      <input type="checkbox" ${it._pick ? 'checked' : ''} ${it.in_deck ? 'disabled' : ''} onchange="_vItems[${i}]._pick=this.checked;renderVocabReview()">
      <div class="tb-vrow-main">${it.romanization ? `<div class="tb-vrow-rom">${esc(it.romanization)}</div>` : ''}
        <div class="tb-vrow-word">${esc(it.target)} <span class="gl">— ${esc(it.gloss)}</span></div></div>
      ${it.in_deck ? '<span class="tb-vbadge have">✓ in deck</span>' : (it.cefr ? `<span class="tb-vbadge">${esc(it.cefr)}</span>` : '')}
    </label>`).join('');
    document.getElementById('tb-sheet').innerHTML = `<h2>Build vocab deck</h2>
      <p class="tb-sub">Every word ${_vMeta.section ? `from <b>${esc(_vMeta.section)}</b>` : 'in these pages'}. Uncheck any you don't want.</p>
      <div class="tb-vstats">
        <div class="tb-vstat new"><b>${newC}</b><span>new</span></div>
        <div class="tb-vstat"><b>${inDeck}</b><span>in deck</span></div>
        <div class="tb-vstat"><b>${total}</b><span>found</span></div></div>
      <div class="tb-vselbar"><span>${sel} selected</span>
        <span><button onclick="_vItems.forEach(it=>it._pick=!it.in_deck);renderVocabReview()">Select all new</button> ·
          <button onclick="_vItems.forEach(it=>it._pick=false);renderVocabReview()">Clear</button></span></div>
      <div class="tb-vlist">${rows}</div>
      <label class="tb-field"><span>Deck name</span><input class="tb-input" id="v-name" value="${esc(_vName)}" maxlength="80"></label>
      <div id="v-status"></div>
      <div class="tb-row-actions">
        <button class="tb-btn ghost" onclick="closeModal()">Cancel</button>
        <button class="tb-btn" id="v-go" ${sel ? '' : 'disabled'} onclick="commitVocab()">Add ${sel} to deck</button>
      </div>`;
  }

  async function commitVocab() {
    if (_busy) return;
    const picks = _vItems.filter(it => it._pick && !it.in_deck);
    const name = (document.getElementById('v-name').value || '').trim();
    if (!picks.length) { document.getElementById('v-status').innerHTML = '<div class="tb-error">Select at least one new word.</div>'; return; }
    if (!name) { document.getElementById('v-status').innerHTML = '<div class="tb-error">Give the deck a name.</div>'; return; }
    _busy = true; document.getElementById('v-go').disabled = true;
    document.getElementById('v-status').innerHTML = '<div class="tb-progress-note"><span class="spinner"></span> Adding words…</div>';
    try {
      const r = await fetch('/api/vocab-deck', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lang: _vMeta.lang, deck_name: name,
          items: picks.map(it => ({ target_text: it.target, source_text: it.gloss,
            romanization: it.romanization || '', cefr_level: it.cefr || null, notes: it.example || null })) }) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || 'Could not build the deck.');
      openModal(`<div class="tb-vloading"><div style="font-size:2.4rem">📇</div>
        <h3>Added ${data.created} word${data.created === 1 ? '' : 's'}</h3>
        <p>Tagged 📕 ${esc(name)}. Study them any time from Cards.</p>
        <div class="tb-row-actions" style="justify-content:center;margin-top:14px">
          <button class="tb-btn ghost" onclick="closeModal()">Keep reading</button>
          ${data.label_id ? `<a class="tb-btn" href="/cards?label_id=${data.label_id}&study=1" style="text-decoration:none">Study ${data.created} →</a>` : ''}
        </div></div>`);
    } catch (e) {
      document.getElementById('v-status').innerHTML = `<div class="tb-error">${esc(e.message || 'Could not build the deck.')}</div>`;
      const go = document.getElementById('v-go'); if (go) go.disabled = false;
    } finally { _busy = false; }
  }

  // ── Modal helpers ─────────────────────────────────────────────────────────
  // The sheet is bottom-anchored, so the phone keyboard would sit on top of the
  // very buttons a form needs. Shrink the overlay to the VISIBLE viewport so its
  // flex-end lands above the keyboard (iOS never resizes the layout viewport).
  function _syncModalViewport() {
    const vv = window.visualViewport;
    const modal = document.getElementById('tb-modal');
    if (!vv || !modal) return;
    const overlap = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
    modal.style.bottom = overlap ? overlap + 'px' : '';
  }
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', _syncModalViewport);
    window.visualViewport.addEventListener('scroll', _syncModalViewport);
  }

  function openModal(html) {
    document.getElementById('tb-sheet').innerHTML = html;
    document.getElementById('tb-modal').classList.add('open');
    _syncModalViewport();
  }
  function closeModal() {
    document.getElementById('tb-modal').classList.remove('open');
    document.getElementById('tb-modal').style.bottom = '';
  }

  window.addEventListener('pagehide', () => { if (_rb) saveReading(true); });
  boot();
