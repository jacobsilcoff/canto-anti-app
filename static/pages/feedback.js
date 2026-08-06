
  function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

  let _fbType = 'bug';
  let _isAdmin = false;
  let _adminFilter = '';

  function setType(t) {
    _fbType = t;
    document.getElementById('type-bug').classList.toggle('active', t === 'bug');
    document.getElementById('type-feature').classList.toggle('active', t === 'feature');
  }

  function onScreenshot(input) {
    const file = input.files?.[0];
    const preview = document.getElementById('fb-screenshot-preview');
    const name = document.getElementById('fb-screenshot-name');
    if (file) {
      preview.src = URL.createObjectURL(file);
      preview.style.display = 'block';
      name.textContent = file.name;
    } else {
      preview.style.display = 'none';
      name.textContent = '';
    }
  }

  async function submitFeedback() {
    const title = document.getElementById('fb-title').value.trim();
    if (!title) { showToast('Please enter a title'); return; }
    const btn = document.getElementById('fb-submit');
    btn.disabled = true;
    btn.textContent = 'Submitting…';
    const fd = new FormData();
    fd.append('type', _fbType);
    fd.append('title', title);
    fd.append('description', document.getElementById('fb-desc').value.trim());
    const file = document.getElementById('fb-screenshot-input').files?.[0];
    if (file) fd.append('screenshot', file);
    try {
      const res = await fetch('/api/feedback', { method: 'POST', body: fd });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Submit failed');
      showToast(data.message || 'Submitted!');
      document.getElementById('fb-title').value = '';
      document.getElementById('fb-desc').value = '';
      document.getElementById('fb-screenshot-input').value = '';
      document.getElementById('fb-screenshot-preview').style.display = 'none';
      document.getElementById('fb-screenshot-name').textContent = '';
      loadMyReports();
      if (_isAdmin) loadAdminReports();
    } catch (e) { showToast(e.message || 'Failed'); }
    btn.disabled = false;
    btn.textContent = 'Submit';
  }

  async function loadMyReports() {
    try {
      const data = await fetch('/api/feedback/mine').then(r => r.json());
      const list = document.getElementById('fb-my-list');
      if (!data.reports?.length) {
        list.innerHTML = '<div class="fb-empty">No reports yet</div>';
        return;
      }
      list.innerHTML = data.reports.map(r => `
        <div class="fb-item">
          <div class="fb-item-icon">${r.type === 'bug' ? '🐛' : '💡'}</div>
          <div class="fb-item-body">
            <div class="fb-item-title">${esc(r.title)}</div>
            <div class="fb-item-meta">${new Date(r.created_at + 'Z').toLocaleDateString()} · <span class="fb-status ${r.status}">${r.status.replace('_', ' ')}</span></div>
          </div>
        </div>`).join('');
    } catch { document.getElementById('fb-my-list').innerHTML = '<div class="fb-empty">Could not load</div>'; }
  }

  async function loadAdminReports() {
    try {
      const url = '/api/admin/feedback' + (_adminFilter ? `?status=${_adminFilter}` : '');
      const data = await fetch(url).then(r => r.json());
      const list = document.getElementById('fb-admin-list');
      if (!data.reports?.length) {
        list.innerHTML = '<div class="fb-empty">No reports</div>';
        return;
      }
      list.innerHTML = data.reports.map(r => `
        <div class="fb-item" style="cursor:pointer" onclick="openDetail(${r.id})">
          <div class="fb-item-icon">${r.type === 'bug' ? '🐛' : '💡'}</div>
          <div class="fb-item-body">
            <div class="fb-item-title">${esc(r.title)}</div>
            <div class="fb-item-meta">
              ${esc(r.username)} · ${new Date(r.created_at + 'Z').toLocaleDateString()}
              · <span class="fb-status ${r.status}">${r.status.replace('_', ' ')}</span>
              · <span class="fb-priority ${r.priority}">${r.priority}</span>
              ${r.triage_group ? ` · <span class="fb-group-pill">${esc(r.triage_group)}</span>` : ''}
            </div>
          </div>
        </div>`).join('');
    } catch {}
  }

  function filterReports(btn, status) {
    _adminFilter = status;
    document.querySelectorAll('#fb-admin-filters .fb-filter-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    loadAdminReports();
  }

  async function openDetail(id) {
    try {
      const r = await fetch(`/api/admin/feedback/${id}`).then(r => r.json());
      document.getElementById('detail-title').textContent = r.title;
      const screenshot = r.screenshot_media_id
        ? `<div class="fb-detail-field"><div class="fb-detail-field-label">Screenshot</div><img class="fb-screenshot-img" src="/api/media/${esc(r.screenshot_media_id)}.jpg" onclick="window.open(this.src)"></div>` : '';
      const prompt = r.suggested_prompt
        ? `<div class="fb-detail-field"><div class="fb-detail-field-label">Suggested Claude prompt <button class="fb-copy-btn" onclick="copyPrompt(this)">Copy</button></div><div class="fb-prompt-box" id="detail-prompt">${esc(r.suggested_prompt)}</div></div>` : '';
      document.getElementById('detail-body').innerHTML = `
        <div class="fb-detail-field">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <span class="fb-priority ${r.priority}">${r.priority}</span>
            <span class="fb-status ${r.status}">${r.status.replace('_', ' ')}</span>
            ${r.triage_group ? `<span class="fb-group-pill">${esc(r.triage_group)}</span>` : ''}
            <span style="font-size:0.78rem;color:var(--text-muted)">${esc(r.username)} · ${r.type === 'bug' ? '🐛 Bug' : '💡 Feature'}</span>
          </div>
        </div>
        <div class="fb-detail-field">
          <div class="fb-detail-field-label">Description</div>
          <div class="fb-detail-field-value">${esc(r.description || '(none)')}</div>
        </div>
        ${screenshot}
        ${r.triage_summary ? `<div class="fb-detail-field"><div class="fb-detail-field-label">Triage summary</div><div class="fb-detail-field-value">${esc(r.triage_summary)}</div></div>` : ''}
        ${prompt}
        <div class="fb-detail-field">
          <div class="fb-detail-field-label">Update status</div>
          <div style="display:flex;gap:8px;align-items:center">
            <select class="settings-select fb-admin-status-select" id="detail-status">
              <option value="new" ${r.status === 'new' ? 'selected' : ''}>New</option>
              <option value="triaged" ${r.status === 'triaged' ? 'selected' : ''}>Triaged</option>
              <option value="in_progress" ${r.status === 'in_progress' ? 'selected' : ''}>In progress</option>
              <option value="resolved" ${r.status === 'resolved' ? 'selected' : ''}>Resolved</option>
              <option value="closed" ${r.status === 'closed' ? 'selected' : ''}>Closed</option>
            </select>
            <button class="fb-copy-btn" onclick="updateStatus(${r.id})">Update</button>
          </div>
        </div>
        <div class="fb-detail-field">
          <div class="fb-detail-field-label">Admin notes</div>
          <textarea class="fb-input" id="detail-notes" rows="2" style="font-size:0.85rem">${esc(r.admin_notes || '')}</textarea>
        </div>
      `;
      document.getElementById('fb-detail-overlay').style.display = 'flex';
    } catch { showToast('Could not load report'); }
  }

  function closeDetail() {
    document.getElementById('fb-detail-overlay').style.display = 'none';
    loadAdminReports();
  }

  async function updateStatus(id) {
    const status = document.getElementById('detail-status').value;
    const notes = document.getElementById('detail-notes').value.trim();
    try {
      const res = await fetch(`/api/admin/feedback/${id}/status`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status, admin_notes: notes }),
      });
      if (!res.ok) throw new Error();
      showToast('Updated');
    } catch { showToast('Failed'); }
  }

  function copyPrompt(btn) {
    const text = document.getElementById('detail-prompt')?.textContent;
    if (text) { navigator.clipboard.writeText(text).then(() => { btn.textContent = 'Copied!'; setTimeout(() => btn.textContent = 'Copy', 1500); }); }
  }

  let _toastTimer;
  function showToast(msg) {
    const el = document.getElementById('toast');
    el.textContent = msg;
    el.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => el.classList.remove('show'), 3000);
  }

  // Init
  (async function() {
    loadMyReports();
    try {
      const me = await fetch('/api/me').then(r => r.json());
      if (me.is_admin) {
        _isAdmin = true;
        document.getElementById('fb-admin').style.display = '';
        loadAdminReports();
      }
    } catch {}
  })();
