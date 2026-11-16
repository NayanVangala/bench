// Accounts on the main page: user menu, Save (create, then update the same
// build), and My builds (open / delete). Builds are stored server-side per
// user (/api/builds); nothing here decides a verdict. Opening a build
// re-runs its check.
(function () {
  'use strict';
  function $(id) { return document.getElementById(id); }
  var NAMES = { circuit: 'Circuit', fiber: 'Fiber link', optics: 'Optics bench' };
  var me = null;
  var buildId = null; // the saved build the current design came from / was saved as
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function domain() { return document.body.getAttribute('data-committed'); }
  function flash(msg, bad) {
    var b = $('error-banner');
    b.textContent = msg;
    b.classList.add('show');
    b.style.background = bad ? '' : 'var(--pass)';
    setTimeout(function () { if (b.textContent === msg) { b.classList.remove('show'); b.style.background = ''; } }, bad ? 6000 : 2500);
  }

  fetch('/auth/me').then(function (r) { return r.ok ? r.json() : null; }).then(function (u) {
    me = u;
    $('user-menu').hidden = !u;
    $('login-link').hidden = !!u;
    $('save-build-btn').hidden = !u;
    if (u) {
      $('avatar-btn').textContent = (u.name || u.email).trim().charAt(0).toUpperCase();
      $('menu-email').textContent = u.email;
    }
  }).catch(function () {});

  // ---- menu ----
  function setMenu(open) {
    $('account-menu').hidden = !open;
    $('avatar-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  $('avatar-btn').addEventListener('click', function (e) { e.stopPropagation(); setMenu($('account-menu').hidden); });
  document.addEventListener('click', function (e) { if (!$('user-menu').contains(e.target)) setMenu(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') setMenu(false); });
  $('logout-btn').addEventListener('click', async function () {
    await fetch('/auth/logout', { method: 'POST' });
    location.replace('/login');
  });

  // ---- save ----
  function snapshot() {
    var d = domain();
    if (d === 'circuit') {
      var nl = window.NetproofPage ? window.NetproofPage.getNetlist() : [];
      var rep = window.NetproofPage ? window.NetproofPage.getReport() : null;
      return { domain: d, data: { components: nl }, verdict: rep && rep.verdict, title: nl.length ? nl.length + '-part circuit' : 'Circuit' };
    }
    var api = d === 'fiber' ? window.NetproofFiber : window.NetproofOptics;
    var design = api && api.get();
    if (!design) return null;
    var verdictEl = $(d + '-verdict');
    return { domain: d, data: design, verdict: (verdictEl && verdictEl.textContent) || null, title: design.title || NAMES[d] };
  }
  $('save-build-btn').addEventListener('click', async function () {
    var s = snapshot();
    if (!s || (s.domain === 'circuit' && !s.data.components.length)) { flash('Nothing to save yet.', true); return; }
    var title = $('build-title').value.trim() || s.title;
    $('build-title').value = title;
    var body = { domain: s.domain, title: title.slice(0, 200), verdict: s.verdict || null, data: s.data };
    var res = await fetch(buildId ? '/api/builds/' + buildId : '/api/builds', {
      method: buildId ? 'PUT' : 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    });
    var data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) { flash(data.message || 'Save failed (' + res.status + ').', true); return; }
    buildId = data.id;
    $('save-build-btn').textContent = 'Saved ✓';
    setTimeout(function () { $('save-build-btn').textContent = 'Save'; }, 1800);
  });
  window.addEventListener('bench:new-build', function () { buildId = null; $('build-title').value = ''; });

  // ---- my builds ----
  var dlg = $('builds-dialog');
  async function openBuilds() {
    setMenu(false);
    $('builds-list').innerHTML = '<li class="empty">Loading…</li>';
    dlg.showModal();
    var res = await fetch('/api/builds');
    if (!res.ok) { $('builds-list').innerHTML = '<li class="empty">Could not load your builds (' + res.status + ').</li>'; return; }
    var list = await res.json();
    if (!list.length) { $('builds-list').innerHTML = '<li class="empty">No saved builds yet. Use Save while working on a design.</li>'; return; }
    $('builds-list').innerHTML = list.map(function (b) {
      var v = (b.verdict || '').toLowerCase();
      return '<li><div class="b-main"><span class="b-title">' + esc(b.title) + '</span><span class="b-meta mono">' + esc(NAMES[b.domain] || b.domain) +
        ' · ' + esc(new Date(b.updated_at).toLocaleString()) + '</span></div>' +
        (b.verdict ? '<span class="sev sev-' + esc(v === 'pass' ? 'pass' : v === 'warn' ? 'warn' : 'stop') + '">' + esc(b.verdict) + '</span>' : '') +
        '<button type="button" class="ghost-btn" data-open="' + b.id + '">Open</button>' +
        '<button type="button" class="row-del" data-delete="' + b.id + '" aria-label="Delete ' + esc(b.title) + '">&times;</button></li>';
    }).join('');
  }
  $('my-builds-btn').addEventListener('click', openBuilds);
  $('builds-close').addEventListener('click', function () { dlg.close(); });
  $('builds-list').addEventListener('click', async function (e) {
    var open = e.target.getAttribute('data-open');
    var del = e.target.getAttribute('data-delete');
    if (del) {
      // Two-step delete: the first press arms the button, the second deletes.
      if (!e.target.classList.contains('armed')) {
        e.target.classList.add('armed');
        e.target.textContent = 'Delete?';
        setTimeout(function () { e.target.classList.remove('armed'); e.target.innerHTML = '&times;'; }, 3000);
        return;
      }
      var r = await fetch('/api/builds/' + del, { method: 'DELETE' });
      if (r.ok) { if (String(buildId) === del) buildId = null; openBuilds(); }
      return;
    }
    if (!open) return;
    var res = await fetch('/api/builds/' + open);
    if (!res.ok) { flash('Could not open that build.', true); return; }
    var b = await res.json();
    dlg.close();
    window.NetproofStart.commit(b.domain);
    if (b.domain === 'circuit') {
      window.NetproofPage.applyNetlist(b.data.components || []);
      window.NetproofPage.recheck();
    } else {
      (b.domain === 'fiber' ? window.NetproofFiber : window.NetproofOptics).load(b.data);
    }
    buildId = b.id;
    $('build-title').value = b.title;
  });
})();
