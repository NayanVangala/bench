// Optics bench mode: examples, element editor, top-view plan, verdict,
// findings, build pack, and the shared 3D bench (main3d setOpticsBench).
// Every number and beam comes from POST /optics/check; nothing is traced here.
(function () {
  'use strict';
  var EXAMPLES = {
 "good": {
  "title": "Periscope steer, lens, ND filter, detector",
  "width_mm": 600,
  "depth_mm": 300,
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "x_mm": 50,
    "y_mm": 50,
    "angle_deg": 0,
    "wavelength_nm": 532,
    "power_mw": 4.5,
    "beam_mm": 1.0
   },
   {
    "id": "M1",
    "type": "mirror",
    "x_mm": 300,
    "y_mm": 50,
    "angle_deg": 135
   },
   {
    "id": "M2",
    "type": "mirror",
    "x_mm": 300,
    "y_mm": 250,
    "angle_deg": 135
   },
   {
    "id": "LN1",
    "type": "lens",
    "x_mm": 400,
    "y_mm": 250,
    "angle_deg": 0,
    "focal_mm": 100
   },
   {
    "id": "ND1",
    "type": "filter",
    "x_mm": 470,
    "y_mm": 250,
    "angle_deg": 0,
    "od": 1
   },
   {
    "id": "D1",
    "type": "detector",
    "x_mm": 550,
    "y_mm": 250,
    "angle_deg": 0,
    "max_mw": 10
   }
  ]
 },
 "stray": {
  "title": "50:50 split with an unblocked arm",
  "width_mm": 600,
  "depth_mm": 300,
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "x_mm": 50,
    "y_mm": 150,
    "angle_deg": 0,
    "wavelength_nm": 633,
    "power_mw": 4,
    "beam_mm": 1.0
   },
   {
    "id": "BS1",
    "type": "beamsplitter",
    "x_mm": 250,
    "y_mm": 150,
    "angle_deg": 135,
    "split": 0.5
   },
   {
    "id": "D1",
    "type": "detector",
    "x_mm": 520,
    "y_mm": 150,
    "angle_deg": 0,
    "max_mw": 10
   }
  ]
 },
 "misaligned": {
  "title": "Mirror at the wrong angle",
  "width_mm": 600,
  "depth_mm": 300,
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "x_mm": 50,
    "y_mm": 50,
    "angle_deg": 0,
    "wavelength_nm": 633,
    "power_mw": 1,
    "beam_mm": 1.0
   },
   {
    "id": "M1",
    "type": "mirror",
    "x_mm": 300,
    "y_mm": 50,
    "angle_deg": 120
   },
   {
    "id": "D1",
    "type": "detector",
    "x_mm": 300,
    "y_mm": 250,
    "angle_deg": 90,
    "max_mw": 10
   }
  ]
 },
 "overload": {
  "title": "20 mW laser straight into a detector",
  "width_mm": 600,
  "depth_mm": 300,
  "elements": [
   {
    "id": "L1",
    "type": "laser",
    "x_mm": 50,
    "y_mm": 150,
    "angle_deg": 0,
    "wavelength_nm": 532,
    "power_mw": 20,
    "beam_mm": 1.0
   },
   {
    "id": "LN1",
    "type": "lens",
    "x_mm": 250,
    "y_mm": 150,
    "angle_deg": 0,
    "focal_mm": 100
   },
   {
    "id": "D1",
    "type": "detector",
    "x_mm": 450,
    "y_mm": 150,
    "angle_deg": 0,
    "max_mw": 10
   }
  ]
 }
};
  var DEFAULTS = {
    mirror: { angle_deg: 135 }, beamsplitter: { angle_deg: 135, split: 0.5 }, lens: { angle_deg: 0, focal_mm: 100 },
    filter: { angle_deg: 0, od: 1 }, dump: { angle_deg: 0 }, detector: { angle_deg: 0, max_mw: 10 }
  };
  var PARAMS = {
    laser: [['power_mw', 'mW'], ['wavelength_nm', 'nm']], mirror: [], beamsplitter: [['split', 'R']],
    lens: [['focal_mm', 'f mm']], filter: [['od', 'OD']], dump: [], detector: [['max_mw', 'max mW']]
  };
  var bench = null;
  var report = null;
  function $(id) { return document.getElementById(id); }
  function active() { return document.body.getAttribute('data-domain') === 'optics'; }
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  function show3d() {
    var h = window.__netproof3d;
    if (active() && bench && h && typeof h.setOpticsBench === 'function') h.setOpticsBench(bench, report);
  }
  window.addEventListener('netproof:3d-ready', show3d);

  async function check() {
    if (!bench) return;
    var btn = $('optics-check-btn');
    btn.disabled = true;
    try {
      var res = await fetch('/optics/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bench) });
      var data = await res.json();
      if (!res.ok) { report = null; showError(data.message || ('Trace failed (' + res.status + ')')); }
      else { report = data; $('error-banner').classList.remove('show'); }
    } catch (e) { report = null; showError('Network error: ' + e.message); }
    finally { btn.disabled = false; }
    renderAll();
    show3d();
  }
  function showError(msg) { var b = $('error-banner'); b.textContent = msg; b.classList.add('show'); }
  function loadExample(key) { bench = JSON.parse(JSON.stringify(EXAMPLES[key])); report = null; check(); }

  function renderAll() { renderVerdict(); renderFindings(); renderPlan(); renderTable(); renderChips(); }

  function renderVerdict() {
    var v = report ? report.verdict : null;
    var el = $('optics-verdict');
    el.textContent = v || '';
    el.className = 'verdict-word' + (v ? ' ' + v.toLowerCase() : '');
    $('optics-verdict-empty').hidden = !!v;
    $('optics-verdict-card').setAttribute('data-verdict', v || '');
    $('optics-pack-cta').hidden = !(v === 'PASS' || v === 'WARN');
    var rx = report ? Object.keys(report.received_mw).map(function (k) { return report.received_mw[k]; }) : [];
    $('optics-rx').textContent = !report ? '—' : rx.length ? rx.map(function (p) { return p.toPrecision(3) + ' mW'; }).join(', ') : '0 mW';
    $('optics-stray').textContent = report ? String(report.stray_beams) : '—';
    $('optics-segs').textContent = report ? String(report.segments.length) : '—';
  }

  function renderFindings() {
    var ul = $('optics-findings');
    if (!report) { ul.innerHTML = '<li class="empty">No findings yet. Run a check first.</li>'; return; }
    if (!report.findings.length) { ul.innerHTML = '<li class="empty">No rules fired.</li>'; return; }
    ul.innerHTML = report.findings.map(function (f) {
      var c = f.severity === 'STOP' ? 'sev-stop' : f.severity === 'WARN' ? 'sev-warn' : 'sev-info';
      return '<li class="' + c + '"><div class="findings-head"><span class="sev ' + c + '">' + esc(f.severity) + '</span> ' +
        '<span class="findings-code">' + esc(f.rule_id) + '</span></div><p class="findings-msg">' + esc(f.message) + '</p></li>';
    }).join('');
  }

  // Top view (y up), beams from the report's segments only.
  function renderPlan() {
    var box = $('optics-plan');
    if (!bench) return;
    var W = bench.width_mm || 600, D = bench.depth_mm || 300, s = 340 / W, H = D * s;
    function X(x) { return x * s; }
    function Y(y) { return H - y * s; }
    var bad = {};
    if (report) report.findings.forEach(function (f) { if (f.severity === 'STOP') (f.component_ids || []).forEach(function (id) { bad[id] = true; }); });
    var svg = '<svg viewBox="0 0 340 ' + H.toFixed(1) + '">';
    for (var x = 0; x <= W; x += 25) for (var y = 0; y <= D; y += 25) svg += '<circle class="hole" cx="' + X(x).toFixed(1) + '" cy="' + Y(y).toFixed(1) + '" r="1.1"/>';
    if (report) {
      var maxP = Math.max.apply(null, report.segments.map(function (g) { return g.power_mw; }).concat([1e-9]));
      report.segments.forEach(function (g) {
        var f = Math.max(0.2, Math.sqrt(g.power_mw / maxP));
        svg += '<line class="beam" x1="' + X(g.from[0]) + '" y1="' + Y(g.from[1]) + '" x2="' + X(g.to[0]) + '" y2="' + Y(g.to[1]) +
          '" stroke="' + (g.end === 'edge' ? '#ff4d4d' : '#3cff6e') + '" stroke-width="' + (1 + 2 * f).toFixed(2) + '" stroke-opacity="' + (0.4 + 0.6 * f).toFixed(2) + '"' +
          (g.end === 'edge' ? ' stroke-dasharray="4 3"' : '') + '/>';
      });
    }
    bench.elements.forEach(function (e) {
      var r = (e.angle_deg || 0) * Math.PI / 180, cx = X(e.x_mm), cy = Y(e.y_mm);
      var nx = Math.cos(r), ny = -Math.sin(r), tx = -ny, ty = nx, half = 6;
      if (e.type === 'laser') {
        svg += '<rect class="el" x="' + (cx - 5) + '" y="' + (cy - 3) + '" width="10" height="6" transform="rotate(' + (-(e.angle_deg || 0)) + ' ' + cx + ' ' + cy + ')"/>';
      } else {
        svg += '<line x1="' + (cx - tx * half) + '" y1="' + (cy - ty * half) + '" x2="' + (cx + tx * half) + '" y2="' + (cy + ty * half) +
          '" stroke="' + (bad[e.id] ? '#ff5a5a' : '#e9edf3') + '" stroke-width="3" stroke-linecap="round"/>';
      }
      svg += '<text class="lbl" x="' + (cx + 7) + '" y="' + (cy - 7) + '">' + esc(e.id) + '</text>';
    });
    box.innerHTML = svg + '</svg>';
  }

  function num(i, k, v, label) {
    return '<input type="number" step="any" data-i="' + i + '" data-k="' + k + '" value="' + esc(v == null ? '' : v) + '" aria-label="' + esc(label) + '">';
  }
  // Re-rendering replaces the inputs; keep keyboard focus on the same field.
  function keepFocus(render) {
    var a = document.activeElement;
    var key = a && a.closest && a.closest('#optics-body') ? [a.getAttribute('data-i'), a.getAttribute('data-k')] : null;
    render();
    if (key && key[0] != null) {
      var el = document.querySelector('#optics-body [data-i="' + key[0] + '"][data-k="' + key[1] + '"]');
      if (el) el.focus();
    }
  }
  function renderTable() { keepFocus(renderTableRows); }
  function renderTableRows() {
    if (!bench) return;
    $('optics-body').innerHTML = bench.elements.map(function (e, i) {
      var params = (PARAMS[e.type] || []).map(function (p) {
        return '<label class="fld">' + num(i, p[0], e[p[0]], e.id + ' ' + p[1]) + '<span>' + esc(p[1]) + '</span></label>';
      }).join(' ');
      return '<tr><td class="mono">' + esc(e.id) + '</td><td>' + esc(e.type) + '</td><td>' + num(i, 'x_mm', e.x_mm, e.id + ' x') + '</td><td>' +
        num(i, 'y_mm', e.y_mm, e.id + ' y') + '</td><td>' + num(i, 'angle_deg', e.angle_deg, e.id + ' angle') + '</td><td class="settings">' +
        (params || '<span class="empty">—</span>') +
        ' <button type="button" class="rot" data-rot="' + i + '" data-by="45" aria-label="Rotate ' + esc(e.id) + ' 45 degrees counter-clockwise">⟲</button>' +
        '<button type="button" class="rot" data-rot="' + i + '" data-by="-45" aria-label="Rotate ' + esc(e.id) + ' 45 degrees clockwise">⟳</button></td><td>' +
        (e.type === 'laser' ? '' : '<button type="button" class="row-del" data-del="' + i + '" aria-label="Remove ' + esc(e.id) + '">&times;</button>') + '</td></tr>';
    }).join('');
  }

  function renderChips() {
    if (!active()) return;
    $('chip-parts').textContent = (bench ? bench.elements.length : 0) + ' ELEMENTS';
    $('chip-ngspice').textContent = 'RAY TRACE · 2D';
    var v = report ? report.verdict : null;
    $('verdict-chip-text').textContent = 'VERDICT · ' + (v || '—');
    $('verdict-dot').className = 'vdot' + (v ? ' ' + v.toLowerCase() : '');
  }

  function nextId(type) {
    var p = { mirror: 'M', beamsplitter: 'BS', lens: 'LN', filter: 'ND', dump: 'BD', detector: 'D' }[type] || 'X';
    var n = 1;
    while (bench.elements.some(function (e) { return e.id === p + n; })) n++;
    return p + n;
  }
  $('optics-body').addEventListener('change', function (ev) {
    var t = ev.target, i = t.getAttribute('data-i'), k = t.getAttribute('data-k');
    if (i == null || !k) return;
    bench.elements[Number(i)][k] = t.value === '' ? null : Number(t.value);
    check();
  });
  $('optics-body').addEventListener('click', function (ev) {
    var rot = ev.target.getAttribute && ev.target.getAttribute('data-rot');
    if (rot != null) {
      var el = bench.elements[Number(rot)];
      el.angle_deg = (((el.angle_deg || 0) + Number(ev.target.getAttribute('data-by'))) % 360 + 360) % 360;
      check();
      return;
    }
    var d = ev.target.getAttribute && ev.target.getAttribute('data-del');
    if (d == null) return;
    bench.elements.splice(Number(d), 1);
    check();
  });
  $('optics-add-btn').addEventListener('click', function () {
    if (!bench) return;
    var type = $('optics-add-type').value;
    bench.elements.push(Object.assign({ id: nextId(type), type: type, x_mm: (bench.width_mm || 600) / 2, y_mm: (bench.depth_mm || 300) / 2 }, DEFAULTS[type]));
    check();
  });

  async function buildPack() {
    try {
      var res = await fetch('/optics/build-pack', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(bench) });
      if (!res.ok) { var data = {}; try { data = await res.json(); } catch (e) {} showError(data.message || ('Build pack failed (' + res.status + ')')); return; }
      var blob = await res.blob();
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'netproof-optics-build-pack.pdf';
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    } catch (e) { showError('Network error: ' + e.message); }
  }

  document.querySelectorAll('[data-optics-example]').forEach(function (b) {
    b.addEventListener('click', function () { loadExample(b.getAttribute('data-optics-example')); });
  });
  $('optics-check-btn').addEventListener('click', check);
  window.NetproofDesign('optics',
    function (design, rep) { bench = design; report = rep; renderAll(); show3d(); },
    function () {
      bench = { title: 'New optics bench', width_mm: 600, depth_mm: 300, elements: [
        { id: 'L1', type: 'laser', x_mm: 50, y_mm: 150, angle_deg: 0, wavelength_nm: 633, power_mw: 1, beam_mm: 1 },
        { id: 'D1', type: 'detector', x_mm: 550, y_mm: 150, angle_deg: 0, max_mw: 10 }] };
      report = null;
      check();
    });

  // Drag on the 3D breadboard: scene units are 25 mm holes, scene -z is +y.
  // The drag reports the cursor's hit on the floor plane, not the part's own
  // position (parts sit at beam height), so only the CHANGE since the drag
  // started is used. Snap to the hole grid and re-trace once the drag settles.
  var moveTimer = null;
  var drag = null; // { id, x0, z0, xmm, ymm } for the drag in progress
  function onMove(id, pose) {
    if (!bench || !pose) return;
    var e = bench.elements.find(function (el) { return el.id === id; });
    if (!e) return;
    if (!drag || drag.id !== id) drag = { id: id, x0: pose.x, z0: pose.z, xmm: e.x_mm, ymm: e.y_mm };
    var W = bench.width_mm || 600, D = bench.depth_mm || 300;
    var x = Math.round((drag.xmm + (pose.x - drag.x0) * 25) / 25) * 25;
    var y = Math.round((drag.ymm - (pose.z - drag.z0) * 25) / 25) * 25;
    e.x_mm = Math.min(W, Math.max(0, x));
    e.y_mm = Math.min(D, Math.max(0, y));
    clearTimeout(moveTimer);
    moveTimer = setTimeout(function () { drag = null; check(); }, 250);
  }
  $('optics-pack-cta').addEventListener('click', buildPack);

  window.NetproofOptics = {
    onMove: onMove,
    activate: function () { if (!bench) loadExample('good'); else { renderAll(); show3d(); } }
  };
})();
