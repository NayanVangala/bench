// Live knob (circuit mode): select a resistor or a voltage source on the 3D
// bench and drag its value. Each change is re-simulated in ngspice (latest
// value wins); flow speed, LED light, resistor heat and the readouts follow
// the new check. Nothing is estimated in the browser.
(function () {
  'use strict';
  function $(id) { return document.getElementById(id); }
  var E12 = [1, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2];
  var OHMS = [];
  [10, 100, 1000, 10000].forEach(function (d) { E12.forEach(function (m) { OHMS.push(Math.round(m * d * 10) / 10); }); });
  OHMS.push(100000);
  var current = null;   // selected component id
  var pending = null;   // latest value waiting to be checked
  var running = false;

  function page() { return window.NetproofPage; }
  function row(id) { return (page().getNetlist() || []).find(function (r) { return r.id === id; }); }
  function reportRow(id) {
    var rep = page().getReport();
    var rows = (rep && rep.netlist) || [];
    return rows.find(function (r) { return String(r.id).toLowerCase() === String(id).toLowerCase(); });
  }
  function si(v, unit) {
    if (v == null || isNaN(v)) return '—';
    var a = Math.abs(v), p = [[1e3, 'k'], [1, ''], [1e-3, 'm'], [1e-6, 'µ']];
    // Trim zeros only after a decimal point (100 stays 100, 4.70 -> 4.7).
    for (var i = 0; i < p.length; i++) if (a >= p[i][0] || i === p.length - 1) return String(Number((v / p[i][0]).toPrecision(3))) + ' ' + p[i][1] + unit;
  }

  function readouts() {
    if (!current) return;
    var r = reportRow(current);
    $('knob-i').textContent = r ? si(r.current == null ? null : Math.abs(r.current), 'A') : '—';
    $('knob-p').textContent = r ? si(r.power == null ? null : Math.abs(r.power), 'W') : '—';
    var rep = page().getReport();
    $('knob-verdict').textContent = rep && rep.verdict ? rep.verdict : '—';
    $('knob-verdict').className = 'mono knob-v ' + (rep && rep.verdict ? rep.verdict.toLowerCase() : '');
  }

  function open(id) {
    if (document.body.getAttribute('data-domain') !== 'circuit') return;
    var r = row(id);
    if (!r || (r.type !== 'resistor' && r.type !== 'voltage_source')) { close(); return; }
    current = id;
    var s = $('knob-slider');
    if (r.type === 'resistor') {
      s.min = 0; s.max = OHMS.length - 1; s.step = 1;
      var best = 0;
      OHMS.forEach(function (o, i) { if (Math.abs(Math.log(o / r.value)) < Math.abs(Math.log(OHMS[best] / r.value))) best = i; });
      s.value = best;
    } else {
      s.min = 1; s.max = 24; s.step = 0.5; s.value = r.value;
    }
    $('knob-name').textContent = id + ' · ' + (r.type === 'resistor' ? 'resistor' : 'supply');
    $('knob-value').textContent = r.type === 'resistor' ? si(r.value, 'Ω') : r.value + ' V';
    $('knob').hidden = false;
    readouts();
  }
  function close() { current = null; $('knob').hidden = true; }

  async function pump() {
    if (running) return;
    running = true;
    while (pending) {
      var job = pending;
      pending = null;
      page().applyEdit(job.id, { value: job.value });
      try { await page().recheck(); } catch (e) { /* the page shows its own error */ }
      readouts();
    }
    running = false;
  }

  $('knob-slider').addEventListener('input', function () {
    var r = row(current);
    if (!r) return;
    var v = r.type === 'resistor' ? OHMS[Number(this.value)] : Number(this.value);
    $('knob-value').textContent = r.type === 'resistor' ? si(v, 'Ω') : v + ' V';
    pending = { id: current, value: v };
    pump();
  });
  $('knob-close').addEventListener('click', close);
  window.addEventListener('bench:select', function (e) { if (e.detail && e.detail.id) open(e.detail.id); else close(); });
  window.addEventListener('bench:new-build', close);
  setInterval(function () { if (current) readouts(); }, 500);
})();
