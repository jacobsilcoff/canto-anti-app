
  function toggleMobileMenu(){document.getElementById('nav-dropdown').classList.toggle('open')}
  function doLogout(){fetch('/api/logout',{method:'POST'}).catch(function(){}).then(function(){window.location.replace('/login')})}
  document.addEventListener('click',function(e){if(!e.target.closest('header')){var d=document.getElementById('nav-dropdown');if(d)d.classList.remove('open')}});

  function showToast(msg) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.classList.add('show');
    setTimeout(() => t.classList.remove('show'), 4000);
  }

  function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function modelLabel(m) {
    return m
      .replace(/^gemini-/, 'Gemini ')
      .replace('flash-lite', 'Flash Lite')
      .replace(/flash$/, 'Flash')
      .replace(/pro$/, 'Pro');
  }

  async function setPlan(userId, plan) {
    try {
      const res = await fetch(`/api/admin/users/${userId}/plan`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ plan }),
      });
      if (!res.ok) { showToast((await res.json()).detail || 'Failed to change plan.'); return; }
      showToast(plan === 'pro' ? 'Upgraded to Pro.' : 'Reverted to Free.');
      // Update just this row's button in-place so the page doesn't scroll to the top.
      const btn = document.querySelector(`[data-user-plan-btn="${userId}"]`);
      if (btn) {
        if (plan === 'pro') {
          btn.textContent = 'Plan: Pro';
          btn.onclick = () => setPlan(userId, 'free');
        } else {
          btn.textContent = 'Plan: Free';
          btn.onclick = () => setPlan(userId, 'pro');
        }
      }
    } catch { showToast('Failed to change plan.'); }
  }

  async function loadUsers() {
    const list = document.getElementById('user-list');
    list.innerHTML = '<p style="color:var(--text-muted);padding:12px">Loading…</p>';
    try {
      const { users } = await fetch('/api/admin/users').then(r => r.json());
      if (!users.length) {
        list.innerHTML = '<p style="color:var(--text-muted);padding:12px">No users.</p>';
        return;
      }
      list.innerHTML = '';
      users.forEach(u => {
        const row = document.createElement('div');
        row.className = 'user-row';
        const uname = escHtml(u.username).replace(/'/g, "\\'");
        const isPro = u.plan === 'pro';
        const hasStripe = !!u.stripe_customer_id;
        const planBtn = u.is_admin
          ? '<span class="user-meta" style="text-align:center">Unlimited</span>'
          : hasStripe
              ? '<span class="user-meta" style="text-align:center">Pro (Stripe)</span>'
              : (isPro
                  ? `<button class="admin-action-btn" data-user-plan-btn="${u.id}" onclick="setPlan(${u.id}, 'free')">Plan: Pro</button>`
                  : `<button class="admin-action-btn" data-user-plan-btn="${u.id}" onclick="setPlan(${u.id}, 'pro')">Plan: Free</button>`);
        row.innerHTML = `
          <div>
            <div class="user-name">${escHtml(u.username)}${u.is_admin ? '<span class="admin-tag">Admin</span>' : ''}</div>
            <div class="user-meta">Created ${escHtml(u.created_at || '')}</div>
          </div>
          ${planBtn}
          <button class="admin-action-btn" onclick="resetPassword(${u.id}, '${uname}')">Reset password</button>
          <button class="admin-action-btn danger" onclick="deleteUser(${u.id}, '${uname}')">Delete</button>
        `;
        list.appendChild(row);
      });
    } catch {
      list.innerHTML = '<p style="color:var(--again);padding:12px">Failed to load users.</p>';
    }
  }

  async function createUser(e) {
    e.preventDefault();
    const username = document.getElementById('new-username').value.trim();
    const password = document.getElementById('new-password').value;
    const isAdmin = document.getElementById('new-admin').checked;
    if (!username || !password) { showToast('Username and password required.'); return; }
    if (password.length < 6) { showToast('Password must be at least 6 characters.'); return; }
    try {
      const res = await fetch('/api/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, is_admin: isAdmin }),
      });
      if (!res.ok) {
        const msg = (await res.json()).detail || 'Failed to create user.';
        showToast(msg);
        return;
      }
      document.getElementById('new-username').value = '';
      document.getElementById('new-password').value = '';
      document.getElementById('new-admin').checked = false;
      showToast('User created.');
      loadUsers();
    } catch {
      showToast('Network error.');
    }
  }

  async function resetPassword(userId, username) {
    const pw = prompt(`New password for ${username} (min 6 characters):`);
    if (!pw) return;
    if (pw.length < 6) { showToast('Password must be at least 6 characters.'); return; }
    try {
      const res = await fetch(`/api/admin/users/${userId}/password`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: pw }),
      });
      if (!res.ok) throw new Error();
      showToast('Password reset.');
    } catch { showToast('Failed to reset password.'); }
  }

  async function deleteUser(userId, username) {
    if (!confirm(`Delete user "${username}" and ALL their cards, labels, and settings? This cannot be undone.`)) return;
    try {
      const res = await fetch(`/api/admin/users/${userId}`, { method: 'DELETE' });
      if (!res.ok) {
        const msg = (await res.json()).detail || 'Failed to delete user.';
        showToast(msg);
        return;
      }
      showToast('User deleted.');
      loadUsers();
    } catch { showToast('Network error.'); }
  }

  loadUsers();
