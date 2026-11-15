// Fiber link mode: examples, element editor, power-budget chart, verdict,
// findings, build pack, and the shared 3D bench (main3d setFiberLink).
// Every number shown comes from POST /fiber/check; nothing is computed here.
// Describe / upload / start-blank controls shared by the fiber and optics
// panels: POST /<dom>/design (AI proposes, server checks) -> onResult.
window.NetproofDesign = function (dom, onResult, onBlank) {
  function $(id) { return document.getElementById(id); }
  var file = null;
  var input = $(dom + '-file-input');
  $(dom + '-upload-btn').addEventListener('click', function () { input.click(); });
  input.addEventListener('change', function () {
    file = input.files && input.files[0] ? input.files[0] : null;
    var line = $(dom + '-file-name');
    line.hidden = !file;
    line.textContent = file ? 'Image: ' + file.name : '';
    $(dom + '-upload-btn').classList.toggle('has-file', !!file);
  });
  $(dom + '-blank-btn').addEventListener('click', onBlank);
  $(dom + '-design-btn').addEventListener('click', async function () {
    var goal = $(dom + '-goal').value.trim();
    var btn = $(dom + '-design-btn');
    var banner = $('error-banner');
    if (!goal && !file) { banner.textContent = 'Describe the design or upload an image first.'; banner.classList.add('show'); return; }
    var form = new FormData();
    if (goal) form.append('goal', goal);
    if (file) form.append('image', file);
    btn.disabled = true;
    btn.textContent = 'Designing\u2026';
    try {
      var res = await fetch('/' + dom + '/design', { method: 'POST', body: form });
      var data = {};
      try { data = await res.json(); } catch (e) { /* non-JSON error */ }
      if (!res.ok) { banner.textContent = data.message || ('Design failed (' + res.status + ')'); banner.classList.add('show'); return; }
      banner.classList.remove('show');
      onResult(data.design, data.report);
    } catch (e) {
      banner.textContent = 'Network error: ' + e.message;
      banner.classList.add('show');
    } finally {
      btn.disabled = false;
      btn.innerHTML = 'Design it &rarr;';
    }
  });
};

(function () {
  'use strict';
  var EXAMPLES = {
 "good": {
  "title": "1550 nm lab link with 1x2 tap",
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "wavelength_nm": 1550,
    "power_dbm": 0
   },
   {
    "id": "F1",
    "type": "fiber",
    "fiber_type": "smf",
    "length_m": 2
   },
   {
    "id": "C1",
    "type": "connector",
    "polish_a": "UPC",
    "polish_b": "UPC"
   },
   {
    "id": "F2",
    "type": "fiber",
    "fiber_type": "smf",
    "length_m": 10
   },
   {
    "id": "S1",
    "type": "splitter",
    "ports": 2
   },
   {
    "id": "F3",
    "type": "fiber",
    "fiber_type": "smf",
    "length_m": 2
   },
   {
    "id": "C2",
    "type": "connector",
    "polish_a": "UPC",
    "polish_b": "UPC"
   },
   {
    "id": "D1",
    "type": "detector"
   }
  ]
 },
 "overload": {
  "title": "10 mW laser straight into a power meter",
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "wavelength_nm": 1550,
    "power_dbm": 10
   },
   {
    "id": "F1",
    "type": "fiber",
    "fiber_type": "smf",
    "length_m": 1
   },
   {
    "id": "C1",
    "type": "connector",
    "polish_a": "UPC",
    "polish_b": "UPC"
   },
   {
    "id": "D1",
    "type": "detector"
   }
  ]
 },
 "deep_split": {
  "title": "1310 nm, 20 km, 1x64 split",
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "wavelength_nm": 1310,
    "power_dbm": -3
   },
   {
    "id": "C1",
    "type": "connector",
    "polish_a": "UPC",
    "polish_b": "UPC"
   },
   {
    "id": "F1",
    "type": "fiber",
    "fiber_type": "smf",
    "length_m": 20000
   },
   {
    "id": "S1",
    "type": "splitter",
    "ports": 64
   },
   {
    "id": "C2",
    "type": "connector",
    "polish_a": "UPC",
    "polish_b": "UPC"
   },
   {
    "id": "D1",
    "type": "detector",
    "sensitivity_dbm": -28,
    "max_dbm": 0,
    "min_nm": 1260,
    "max_nm": 1620
   }
  ]
 },
 "apc": {
  "title": "APC patch cord into a PC port",
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "wavelength_nm": 1550,
    "power_dbm": 0
   },
   {
    "id": "F1",
    "type": "fiber",
    "fiber_type": "smf",
    "length_m": 3
   },
   {
    "id": "C1",
    "type": "connector",
    "polish_a": "APC",
    "polish_b": "PC"
   },
   {
    "id": "D1",
    "type": "detector"
   }
  ]
 }
};
  var DEFAULTS = {
    fiber: { fiber_type: 'smf', length_m: 2 },
    connector: { polish_a: 'UPC', polish_b: 'UPC' },
    splice: { splice_kind: 'fusion' },
    splitter: { ports: 2 },
    attenuator: { loss_db: 10 },
    isolator: {}
  };
  var FIELDS = {
    laser: [['wavelength_nm', 'nm', 'number'], ['power_dbm', 'dBm', 'number']],
    fiber: [['length_m', 'm', 'number'], ['fiber_type', '', ['smf', 'mmf']]],
    connector: [['polish_a', '', ['UPC', 'PC', 'APC']], ['polish_b', '', ['UPC', 'PC', 'APC']]],
    splice: [['splice_kind', '', ['fusion', 'mechanical']]],
    splitter: [['ports', '1x', 'number']],
    attenuator: [['loss_db', 'dB', 'number']],
    isolator: [],
    detector: [['sensitivity_dbm', 'min dBm', 'number'], ['max_dbm', 'max dBm', 'number']]
  };
  var link = null;
  var report = null;
  var domain = 'circuit';
  function $(id) { return document.getElementById(id); }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function fmtDbm(x) { return x == null ? '—' : x.toFixed(1) + ' dBm'; }

  // ---- domain switch -----------------------------------------------------
  function setDomain(d) {
    domain = d;
    document.body.setAttribute('data-domain', d);
    document.querySelectorAll('.domain-switch [data-domain]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-domain') === d ? 'true' : 'false');
    });
    $('trust-line').textContent = d === 'fiber'
      ? 'Verdicts come from the optical power budget + fixed rules, never the AI.'
      : d === 'optics' ? 'Verdicts come from the ray trace + fixed rules, never the AI.'
      : 'Verdicts come from ngspice + fixed rules, never the AI.';
    var h = window.__netproof3d;
    if (d === 'fiber') {
      if (!link) loadExample('good'); else { renderAll(); show3d(); }
    } else if (d === 'optics') {
      if (window.NetproofOptics) window.NetproofOptics.activate();
    } else if (h && typeof h.setNetlist === 'function' && window.NetproofPage) {
      h.setNetlist(window.NetproofPage.getNetlist(), window.NetproofPage.getReport());
      if (window.NetproofPage.updateStatusStrip) window.NetproofPage.updateStatusStrip();
    }
  }

  function show3d() {
    var h = window.__netproof3d;
    if (domain === 'fiber' && link && h && typeof h.setFiberLink === 'function') h.setFiberLink(link, report);
  }
  window.addEventListener('netproof:3d-ready', show3d);

  // ---- check -------------------------------------------------------------
  async function check() {
    if (!link) return;
    var btn = $('fiber-check-btn');
    btn.disabled = true;
    try {
      var res = await fetch('/fiber/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(link) });
      var data = await res.json();
      if (!res.ok) { report = null; showError(data.message || ('Check failed (' + res.status + ')')); }
      else { report = data; clearError(); }
    } catch (e) {
      report = null;
      showError('Network error: ' + e.message);
    } finally {
      btn.disabled = false;
    }
    renderAll();
    show3d();
  }
  function showError(msg) {
    var b = $('error-banner');
    b.textContent = msg;
    b.classList.add('show');
  }
  function clearError() { $('error-banner').classList.remove('show'); }

  function loadExample(key) {
    link = JSON.parse(JSON.stringify(EXAMPLES[key]));
    report = null;
    check();
  }

  // ---- render ------------------------------------------------------------
  function renderAll() {
    renderVerdict();
    renderFindings();
    renderChart();
    renderTable();
    renderChips();
  }

  function renderVerdict() {
    var v = report ? report.verdict : null;
    var el = $('fiber-verdict');
    el.textContent = v || '';
    el.className = 'verdict-word' + (v ? ' ' + v.toLowerCase() : '');
    $('fiber-verdict-empty').hidden = !!v;
    $('fiber-verdict-card').setAttribute('data-verdict', v || '');
    $('fiber-pack-cta').hidden = !(v === 'PASS' || v === 'WARN');
    var det = link ? link.elements[link.elements.length - 1] : null;
    var sens = det && det.type === 'detector' && det.sensitivity_dbm != null ? det.sensitivity_dbm : -40;
    $('fiber-rx').textContent = report ? fmtDbm(report.received_dbm) : '—';
    $('fiber-loss').textContent = report ? report.total_loss_db.toFixed(2) + ' dB' : '—';
    $('fiber-margin').textContent = report && report.received_dbm != null ? (report.received_dbm - sens).toFixed(1) + ' dB' : '—';
  }

  function renderFindings() {
    var ul = $('fiber-findings');
    if (!report) { ul.innerHTML = '<li class="empty">No findings yet. Run a check first.</li>'; return; }
    if (!report.findings.length) { ul.innerHTML = '<li class="empty">No rules fired.</li>'; return; }
    ul.innerHTML = report.findings.map(function (f) {
      var c = f.severity === 'STOP' ? 'sev-stop' : f.severity === 'WARN' ? 'sev-warn' : 'sev-info';
      return '<li class="' + c + '"><div class="findings-head"><span class="sev ' + c + '">' + esc(f.severity) + '</span> ' +
        '<span class="findings-code">' + esc(f.rule_id) + '</span></div><p class="findings-msg">' + esc(f.message) + '</p></li>';
    }).join('');
  }

  // Step chart: power (dBm) after each element, with the detector's
  // sensitivity and maximum as reference lines. Values from the report only.
  function renderChart() {
    var box = $('fiber-chart');
    if (!report || !report.trace.length) { box.innerHTML = '<p class="empty">Run a check to see the budget.</p>'; return; }
    var det = link.elements[link.elements.length - 1] || {};
    var sens = det.sensitivity_dbm != null ? det.sensitivity_dbm : -40;
    var max = det.max_dbm != null ? det.max_dbm : 3;
    var pts = [{ id: 'in', p: report.launch_dbm }].concat(report.trace.map(function (t) { return { id: t.id, p: t.power_dbm }; }));
    var vals = pts.map(function (q) { return q.p; }).concat([sens, max]);
    var hi = Math.ceil(Math.max.apply(null, vals) + 2), lo = Math.floor(Math.min.apply(null, vals) - 2);
    var W = 340, H = 150, L = 34, R = 8, T = 8, B = 22;
    function x(i) { return L + (W - L - R) * i / Math.max(1, pts.length - 1); }
    function y(p) { return T + (H - T - B) * (hi - p) / (hi - lo); }
    var d = 'M' + x(0) + ' ' + y(pts[0].p);
    for (var i = 1; i < pts.length; i++) d += ' H' + x(i) + ' V' + y(pts[i].p);
    var svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" aria-hidden="true">';
    [hi, Math.round((hi + lo) / 2), lo].forEach(function (g) {
      svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(g) + '" y2="' + y(g) + '" class="grid"/>' +
        '<text x="' + (L - 4) + '" y="' + (y(g) + 3) + '" text-anchor="end" class="axis">' + g + '</text>';
    });
    svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(max) + '" y2="' + y(max) + '" class="ref stop"/>' +
      '<text x="' + (W - R) + '" y="' + (y(max) - 3) + '" text-anchor="end" class="axis stop">max ' + max + '</text>' +
      '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(sens) + '" y2="' + y(sens) + '" class="ref warn"/>' +
      '<text x="' + (W - R) + '" y="' + (y(sens) - 3) + '" text-anchor="end" class="axis warn">sensitivity ' + sens + '</text>' +
      '<path d="' + d + '" class="trace"/>';
    pts.forEach(function (q, k) {
      if (k) svg += '<text x="' + x(k) + '" y="' + (H - 6) + '" text-anchor="middle" class="axis">' + esc(q.id) + '</text>';
    });
    box.innerHTML = svg + '</svg>';
  }

  // Re-rendering replaces the inputs; keep keyboard focus on the same field.
  function keepFocus(render) {
    var a = document.activeElement;
    var key = a && a.closest && a.closest('#fiber-body') ? [a.getAttribute('data-i'), a.getAttribute('data-k')] : null;
    render();
    if (key && key[0] != null) {
      var el = document.querySelector('#fiber-body [data-i="' + key[0] + '"][data-k="' + key[1] + '"]');
      if (el) el.focus();
    }
  }
  function renderTable() { keepFocus(renderTableRows); }
  function renderTableRows() {
    var body = $('fiber-body');
    if (!link) return;
    var trace = {};
    if (report) report.trace.forEach(function (t) { trace[t.id] = t; });
    body.innerHTML = link.elements.map(function (e, i) {
      var inputs = (FIELDS[e.type] || []).map(function (f) {
        var key = f[0], unit = f[1], kind = f[2], val = e[key] == null ? '' : e[key];
        if (Array.isArray(kind)) {
          return '<select data-i="' + i + '" data-k="' + key + '" aria-label="' + esc(e.id + ' ' + key) + '">' +
            kind.map(function (o) { return '<option' + (String(val) === o ? ' selected' : '') + '>' + o + '</option>'; }).join('') + '</select>';
        }
        return '<label class="fld"><input type="number" step="any" data-i="' + i + '" data-k="' + key + '" value="' + esc(val) +
          '" aria-label="' + esc(e.id + ' ' + key) + '"><span>' + esc(unit) + '</span></label>';
      }).join(' ');
      var t = trace[e.id];
      var removable = e.type !== 'laser' && e.type !== 'detector';
      return '<tr><td class="mono">' + esc(e.id) + '</td><td>' + esc(e.type) + '</td><td class="settings">' + (inputs || '<span class="empty">—</span>') +
        '</td><td class="mono">' + (t ? fmtDbm(t.power_dbm) : '—') + '</td><td>' +
        (removable ? '<button type="button" class="row-del" data-del="' + i + '" aria-label="Remove ' + esc(e.id) + '">&times;</button>' : '') + '</td></tr>';
    }).join('');
  }

  function renderChips() {
    if (domain !== 'fiber') return;
    $('chip-parts').textContent = (link ? link.elements.length : 0) + ' ELEMENTS';
    $('chip-ngspice').textContent = 'POWER BUDGET \u00b7 dB';
    var v = report ? report.verdict : null;
    $('verdict-chip-text').textContent = 'VERDICT · ' + (v || '—');
    $('verdict-dot').className = 'vdot' + (v ? ' ' + v.toLowerCase() : '');
  }

  // ---- editing -----------------------------------------------------------
  function nextId(type) {
    var p = { fiber: 'F', connector: 'C', splice: 'J', splitter: 'S', attenuator: 'A', isolator: 'I' }[type] || 'X';
    var n = 1;
    while (link.elements.some(function (e) { return e.id === p + n; })) n++;
    return p + n;
  }
  $('fiber-body').addEventListener('change', function (ev) {
    var t = ev.target, i = t.getAttribute('data-i'), k = t.getAttribute('data-k');
    if (i == null || !k) return;
    var v = t.type === 'number' ? (t.value === '' ? null : Number(t.value)) : t.value;
    link.elements[Number(i)][k] = v;
    check();
  });
  $('fiber-body').addEventListener('click', function (ev) {
    var d = ev.target.getAttribute && ev.target.getAttribute('data-del');
    if (d == null) return;
    link.elements.splice(Number(d), 1);
    check();
  });
  $('fiber-add-btn').addEventListener('click', function () {
    if (!link) return;
    var type = $('fiber-add-type').value;
    var el = Object.assign({ id: nextId(type), type: type }, DEFAULTS[type]);
    link.elements.splice(Math.max(1, link.elements.length - 1), 0, el);
    check();
  });

  // ---- build pack --------------------------------------------------------
  async function buildPack() {
    try {
      var res = await fetch('/fiber/build-pack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(link) });
      if (!res.ok) { var data = {}; try { data = await res.json(); } catch (e) {} showError(data.message || ('Build pack failed (' + res.status + ')')); return; }
      var blob = await res.blob();
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'netproof-fiber-build-pack.pdf';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    } catch (e) { showError('Network error: ' + e.message); }
  }

  document.querySelectorAll('.domain-switch [data-domain]').forEach(function (b) {
    b.addEventListener('click', function () { if (!b.disabled) setDomain(b.getAttribute('data-domain')); });
  });
  document.querySelectorAll('[data-fiber-example]').forEach(function (b) {
    b.addEventListener('click', function () { loadExample(b.getAttribute('data-fiber-example')); });
  });
  $('fiber-check-btn').addEventListener('click', check);
  window.NetproofDesign('fiber',
    function (design, rep) { link = design; report = rep; renderAll(); show3d(); },
    function () {
      link = { title: 'New fiber link', elements: [
        { id: 'L1', type: 'laser', wavelength_nm: 1550, power_dbm: 0 },
        { id: 'F1', type: 'fiber', fiber_type: 'smf', length_m: 2 },
        { id: 'D1', type: 'detector' }] };
      report = null;
      check();
    });
  $('fiber-pack-cta').addEventListener('click', buildPack);
  document.body.setAttribute('data-domain', 'circuit');
})();
