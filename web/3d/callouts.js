/**
 * Netproof 3D callouts — web/3d/callouts.js
 *
 * The blueprint callout system for the Netproof 3D circuit view.
 * One mono callout box per finding, a large verdict callout top-left,
 * a click-to-open detail callout per part, and a Repair diff callout.
 * Every box is joined to its part by a thin SVG leader line with a
 * 3 px dot at the part end. Boxes dock in the canvas edge lanes,
 * sorted by projected anchor height, 8 px gaps, never overlapping
 * each other or the circuit. Layout is recomputed from scratch on
 * every frame (camera move, resize, new Report).
 *
 * This module NEVER invents numbers. Every value it shows comes from
 * the backend Report handed to updateCallouts() (verdict, findings,
 * node_voltages, branch_currents, power, netlist_used) or from the
 * netlist itself (a part's own type/value). A missing value shows
 * "—", never a guess. The verdict word, currents, voltages and power
 * are displayed only; nothing here derives a verdict or a reading.
 * The one arithmetic display is "voltage across", V(n1) - V(n2) from
 * Report node voltages, labelled with the node names exactly as the
 * edit panel does it.
 *
 * Coordinator token decisions (2026-11-15, override the specs where
 * they conflict):
 * - Blueprint blue is #0B3D91 ONLY. No second blue (#1d4ed8, #2563eb,
 *   #2684ff) appears in this file. INFO borders and leaders use it.
 * - Focus ring is #FFD166 (amber) with a #0A2A5E keyline, per the
 *   a11y spec section 2.1. Never #1D4ED8 on a callout.
 * - Severity TEXT colours (AA on white): STOP #991B1B, WARN #92400E,
 *   PASS #166534, INFO #0B3D91, UNKNOWN #4B5563. Muted text #595959.
 * - Callout BORDER colours: STOP #b00020, WARN #b45309, PASS #1a7f37,
 *   INFO #0B3D91, UNKNOWN #4B5563 (the styles.css banner tokens).
 * - Finding and diff callouts are real focusable <button> elements
 *   with a full spoken aria-label. The detail callout is a focusable
 *   <div role="group"> (not a <button>) because it contains real
 *   <button> chips for its findings, and a button may not contain
 *   another button. The verdict callout is a live region, not a
 *   control, so it is a <div> with aria-live (polite, assertive on
 *   STOP) and is not focusable.
 *
 * Report shape (backend/netproof/ir.py — Report):
 *   {
 *     verdict: "PASS" | "WARN" | "STOP", mock?: boolean,
 *     findings: [{ severity: "STOP"|"WARN"|"INFO", rule_id,
 *                  component_ids: [id...], message }],
 *     node_voltages:   { [nodeName]: volts },
 *     branch_currents: { [componentId]: amps },
 *     power:           { [componentId]: watts },
 *     netlist_used:    { components: [{ id, type, value,
 *                  nodes: [n1, n2], rating_watts?, color? }] }
 *   }
 * Aliases tolerated: nodeVoltages, branchCurrents, per_component_power /
 * component_power, and a plain `netlist` array in place of netlist_used.
 *
 * Wiring: overlay.js owns the render loop hooks and delegates here:
 *   initCallouts({ THREE, scene, camera, container,
 *                  onSelect?, onActivate? });
 *   // -> handle { element, svg, layer, update, updateCallouts,
 *   //    layoutCallouts, layout, updatePositions, recompute, dispose }
 *   updateCallouts(report, meshesById, { nodesById, components,
 *                    measuredById });
 *   layoutCallouts(camera);   // every frame, after the camera moves
 *   updatePositions / recompute / layout are aliases of
 *   layoutCallouts (same from-scratch edge-lane layout, no
 *   incremental step); main3d.js's adapter resolves any of them.
 *   openDetailCallout(id); closeDetailCallout();
 *   showRepairDiffs(diffs, { rechecking? }); closeRepairDiffs();
 *   disposeCallouts();
 * A page that prefers this module directly can use the same calls
 * without overlay.js. THREE is NOT imported here; pass it to
 * initCallouts() (or leave it on window.THREE). Without THREE there
 * is no projection: callouts still render, stacked in the left lane
 * in Report order, with no leaders.
 *
 * Selection events: a callout activation dispatches a bubbling
 * CustomEvent "netproof:callout-select" ({ componentId, source })
 * on the container, and calls options.onSelect(componentId, source)
 * when supplied. This module never touches the netlist table or the
 * edit panel; the page / main3d.js owns those and listens for the
 * event. "netproof:callout-activate" ({ componentId, kind }) fires
 * on Enter/Space on a detail callout, for the page to open the edit
 * drawer if it wants to.
 */

// ---------------------------------------------------------------------------
// Tokens (coordinator decisions 2026-11-15 — see header)
// ---------------------------------------------------------------------------

export const CALLOUT_BLUE = '#0B3D91'; // the only blue in this file

export const CALLOUT_SEVERITY_COLORS = {
  STOP: '#b00020',
  WARN: '#b45309',
  PASS: '#1a7f37',
  INFO: '#0B3D91',
  UNKNOWN: '#4B5563',
};

export const SEVERITY_TEXT_COLORS = {
  STOP: '#991B1B',
  WARN: '#92400E',
  PASS: '#166534',
  INFO: '#0B3D91',
  UNKNOWN: '#4B5563',
};

export const MUTED_TEXT_COLOR = '#595959';

const FOCUS_RING = '#FFD166';
const FOCUS_KEYLINE = '#0A2A5E';

const LANE_GAP = 8; // px between stacked callout boxes
const LANE_MARGIN = 8; // px from the canvas edge
const CIRCUIT_PAD = 24; // px kept clear around the circuit bounding box
const VERDICT_TOP = 12;
const VERDICT_LEFT = 12;
const VERDICT_WIDTH = 300;
const CALLOUT_WIDTH = 260;
const DOT_RADIUS = 3;

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------

let THREE = null;
let scene = null;
let camera = null;
let container = null;
let svg = null;
let layer = null;
let styleInjected = false;

let onSelectCb = null;
let onActivateCb = null;

let currentReport = null;
let meshesById = {};
let nodesById = {};
let componentsById = {}; // id -> { id, type, value, nodes, rating_watts, color }
let measuredById = {}; // id -> { voltages: [{node, value}], current, power }

let verdictEl = null;
let verdictReadoutsEl = null;
let verdictWordEl = null;
let lastVerdictSignature = '';

const findingItems = new Map(); // key -> { el, line, dot, finding, componentId }
let lastFindingsSignature = '';
let detail = null; // { id, el, line, dot, chips: Map<ruleId, button> }
let diffItems = new Map(); // componentId -> { el, line, dot, lines, componentId }
let diffRechecking = false;

// ---------------------------------------------------------------------------
// Small helpers — formatting only, never a verdict or a reading
// ---------------------------------------------------------------------------

function isNum(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>\"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

export function lookupCI(map, key) {
  if (!map || key == null) return undefined;
  if (map[key] != null) return map[key];
  const want = String(key).toLowerCase();
  for (const k of Object.keys(map)) {
    if (k.toLowerCase() === want) return map[k];
  }
  return undefined;
}

// Unit formatting for Report numbers, mirroring web/index.html
// (fmtCurrent / fmtPower / fmtVolts). "—" when a value is missing.
export function fmtCurrent(a) {
  if (a == null || !isFinite(Number(a))) return '—';
  const amps = Math.abs(Number(a));
  if (amps >= 1) return amps.toFixed(2) + ' A';
  return (amps * 1000).toFixed(1) + ' mA';
}

export function fmtPower(w) {
  if (w == null || !isFinite(Number(w))) return '—';
  const watts = Number(w);
  if (Math.abs(watts) >= 1) return watts.toFixed(2) + ' W';
  return (watts * 1000).toFixed(0) + ' mW';
}

export function fmtVolts(v) {
  if (v == null || !isFinite(Number(v))) return '—';
  return Number(v).toFixed(2) + ' V';
}

export function fmtResistance(ohms) {
  if (ohms == null || !isFinite(Number(ohms))) return '—';
  const r = Number(ohms);
  if (r >= 1e6) return (r / 1e6).toFixed(2).replace(/\.?0+$/, '') + ' MΩ';
  if (r >= 1e3) return (r / 1e3).toFixed(2).replace(/\.?0+$/, '') + ' kΩ';
  return String(r) + ' Ω';
}

const SI_MULTIPLIERS = {
  '': 1, k: 1e3, K: 1e3, M: 1e6, G: 1e9,
  m: 1e-3, u: 1e-6, 'µ': 1e-6, 'μ': 1e-6, n: 1e-9, p: 1e-12,
};

export function parseSIValue(raw) {
  if (typeof raw === 'number') return isFinite(raw) ? raw : null;
  if (raw == null) return null;
  const text = String(raw).trim();
  if (text === '') return null;
  const m = /^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*([kKMGmunpµμ]?)\s*(?:[a-zA-ZΩ]*)\s*$/.exec(text);
  if (!m) return null;
  const mult = SI_MULTIPLIERS[m[2]];
  if (mult == null) return null;
  const n = Number(m[1]) * mult;
  return isFinite(n) ? n : null;
}

// A component value with its unit, mirroring web/index.html
// fmtValueForType (same shapes: "100 Ω", "9 V", "2.0 V" for an LED Vf).
export function fmtValueForType(type, value) {
  const n = parseSIValue(value);
  if (n == null) return String(value == null ? '' : value);
  const t = String(type || '').toLowerCase();
  if (t.indexOf('resistor') === 0) return fmtResistance(n);
  if (t.indexOf('voltage') === 0) return String(n) + ' V';
  if (t.indexOf('current') === 0) return fmtCurrent(n);
  if (t.indexOf('led') === 0 || t.indexOf('diode') === 0) return n.toFixed(1) + ' V';
  if (t.indexOf('capacitor') === 0) {
    if (n >= 1e-3) return (n * 1e3).toFixed(2).replace(/\.?0+$/, '') + ' mF';
    if (n >= 1e-6) return (n * 1e6).toFixed(2).replace(/\.?0+$/, '') + ' µF';
    if (n >= 1e-9) return (n * 1e9).toFixed(2).replace(/\.?0+$/, '') + ' nF';
    return String(n) + ' F';
  }
  return String(value);
}

// Parse the "measured vs threshold" pair out of a finding's own message
// text (the Report already states both). Same six patterns as
// web/index.html parseMeasured, verbatim. Returns null when the message
// carries no such pair; the message itself is always shown in full.
export function parseMeasured(msg) {
  const text = String(msg || '');
  let m;
  m = /current ([\d.]+)\s*mA .*?(?:exceeds|over) the ([\d.]+)\s*mA limit/.exec(text);
  if (m) return { measured: m[1] + ' mA', limit: m[2] + ' mA' };
  m = /current ([\d.]+)\s*mA is in the ([\d.]+)-([\d.]+) mA warning range/.exec(text);
  if (m) return { measured: m[1] + ' mA', limit: m[2] + '–' + m[3] + ' mA range' };
  m = /current ([\d.]+)\s*mA is below ([\d.]+)\s*mA/.exec(text);
  if (m) return { measured: m[1] + ' mA', limit: m[2] + ' mA minimum' };
  m = /dissipates ([\d.]+)\s*W, over its ([\d.]+)\s*W rating/.exec(text);
  if (m) return { measured: m[1] + ' W', limit: m[2] + ' W rating' };
  m = /dissipates ([\d.]+)\s*W, over 70% of its ([\d.]+)\s*W rating/.exec(text);
  if (m) return { measured: m[1] + ' W', limit: '70% of ' + m[2] + ' W' };
  m = /supplies ([\d.]+)\s*A, over the ([\d.]+)\s*A limit/.exec(text);
  if (m) return { measured: m[1] + ' A', limit: m[2] + ' A' };
  return null;
}

function normRow(r) {
  r = r || {};
  const nodes = Array.isArray(r.nodes) ? r.nodes.slice(0, 2)
    : (typeof r.nodes === 'string' ? r.nodes.split(/[\s,]+/).filter(Boolean)
    : (r.net ? String(r.net).split(/[\s,]+/).filter(Boolean) : []));
  return {
    id: r.id || r.refdes || r.name || '',
    type: r.type || r.kind || '',
    value: r.value != null ? r.value : '',
    nodes,
    rating_watts: r.rating_watts != null ? r.rating_watts
      : (r.power_rating_w != null ? r.power_rating_w : undefined),
    color: r.color || undefined,
  };
}

// Before/after diff of a repair, as plain strings like
// "R1 100 Ω → 470 Ω". Mirrors web/index.html diffNetlists exactly
// (same shapes, same field order) so either copy produces the same
// strings; the page owns diffNetlists and passes its strings to
// showRepairDiffs, this copy exists for callers without the page.
export function diffNetlists(before, after) {
  const beforeById = {};
  const afterById = {};
  (before || []).forEach((r) => {
    const row = normRow(r);
    if (row.id) beforeById[row.id] = row;
  });
  (after || []).forEach((r) => {
    const row = normRow(r);
    if (row.id) afterById[row.id] = row;
  });
  const diffs = [];
  Object.keys(afterById).forEach((id) => {
    const a = afterById[id];
    const b = beforeById[id];
    if (!b) { diffs.push(id + ' added (' + fmtValueForType(a.type, a.value) + ')'); return; }
    const parts = [];
    if (String(a.value) !== String(b.value)) {
      parts.push(fmtValueForType(b.type, b.value) + ' → ' + fmtValueForType(a.type, a.value));
    }
    if (String(a.type) !== String(b.type)) parts.push('type ' + b.type + ' → ' + a.type);
    if (a.nodes.join(' ') !== b.nodes.join(' ')) {
      parts.push('nodes ' + b.nodes.join(' ') + ' → ' + a.nodes.join(' '));
    }
    if (String(a.rating_watts) !== String(b.rating_watts)
      && (a.rating_watts != null || b.rating_watts != null)) {
      parts.push('rating ' + (b.rating_watts != null ? b.rating_watts + ' W' : '—')
        + ' → ' + (a.rating_watts != null ? a.rating_watts + ' W' : '—'));
    }
    if (parts.length) diffs.push(id + ' ' + parts.join(', '));
  });
  Object.keys(beforeById).forEach((id) => {
    if (!afterById[id]) diffs.push(id + ' removed');
  });
  return diffs;
}

// ---------------------------------------------------------------------------
// Severity helpers (display only — the verdict comes from the Report)
// ---------------------------------------------------------------------------

function severityOf(finding) {
  const sev = String((finding && (finding.severity || finding.level)) || '')
    .toUpperCase();
  if (sev === 'STOP' || sev === 'WARN' || sev === 'PASS' || sev === 'INFO') return sev;
  return 'INFO';
}

function hasGlobalSevereFinding(findings) {
  for (const f of findings || []) {
    if (!f || typeof f !== 'object') continue;
    const sev = String(f.severity || f.level || '').toUpperCase();
    if (sev !== 'STOP' && sev !== 'WARN') continue;
    let ids = f.component_ids ?? f.componentIds ?? null;
    let list;
    if (Array.isArray(ids)) list = ids;
    else if (typeof ids === 'string') list = [ids];
    else if (f.component) list = [f.component];
    else if (f.id) list = [f.id];
    else list = [];
    if (!list.length) return true;
  }
  return false;
}

// Worst finding severity touching a component; 'PASS' only when the
// overall verdict is PASS and no severe global finding blocks that
// claim; otherwise 'UNKNOWN'. INFO findings never colour a part.
// Mirrors overlay.js severityForComponent.
function severityForComponentLocal(componentId, findings, overallVerdict) {
  let worst = null;
  for (const f of findings || []) {
    if (!f || typeof f !== 'object') continue;
    const ids = f.component_ids || f.componentIds
      || (f.component ? [f.component] : null)
      || (f.id ? [f.id] : null) || [];
    if (!ids.includes(componentId)) continue;
    const sev = String(f.severity || f.level || '').toUpperCase();
    if (sev === 'STOP') return 'STOP';
    if (sev === 'WARN' && worst !== 'STOP') worst = 'WARN';
  }
  if (worst) return worst;
  if (overallVerdict === undefined || overallVerdict === null) return 'UNKNOWN';
  const verdict = String(overallVerdict).trim().toUpperCase();
  if (verdict === 'PASS' && !hasGlobalSevereFinding(findings)) return 'PASS';
  return 'UNKNOWN';
}

function componentIdsOf(finding) {
  if (!finding || typeof finding !== 'object') return [];
  const ids = finding.component_ids ?? finding.componentIds ?? null;
  if (Array.isArray(ids)) return ids.map((c) => String(c));
  if (typeof ids === 'string') return [ids];
  if (finding.component) return [String(finding.component)];
  if (finding.id) return [String(finding.id)];
  return [];
}

function findingsForComponent(componentId, findings) {
  const out = [];
  for (const f of findings || []) {
    if (componentIdsOf(f).includes(componentId)) out.push(f);
  }
  // STOP first, then WARN, then INFO (Report order is already sorted
  // this way; the sort only guards against an unsorted Report).
  const rank = { STOP: 0, WARN: 1, INFO: 2 };
  out.sort((a, b) => (rank[severityOf(a)] ?? 2) - (rank[severityOf(b)] ?? 2));
  return out;
}

// ---------------------------------------------------------------------------
// Report lookups — key readouts, exactly the web/index.html
// renderKeyReadings lookup (same source, same fallback, same order)
// ---------------------------------------------------------------------------

function componentsFromReport(report) {
  const lists = [];
  if (report.netlist_used && Array.isArray(report.netlist_used.components)) {
    lists.push(report.netlist_used.components);
  }
  if (Array.isArray(report.netlist)) lists.push(report.netlist);
  if (Array.isArray(report.components)) lists.push(report.components);
  const out = [];
  for (const list of lists) {
    for (const c of list) {
      const row = normRow(c);
      if (row.id) out.push(row);
    }
  }
  return out;
}

function keyReadings(report, components) {
  const currents = report.branch_currents || report.branchCurrents || {};
  const powers = report.power || report.per_component_power || report.component_power || {};
  const voltages = report.node_voltages || report.nodeVoltages || {};
  const findings = Array.isArray(report.findings) ? report.findings : [];
  let ledCurrent = null;
  let resistorPower = null;
  for (const r of components) {
    const t = String(r.type || '').toLowerCase();
    if (ledCurrent == null && (t.indexOf('led') === 0 || t.indexOf('diode') === 0)) {
      const c = lookupCI(currents, r.id);
      if (c != null) ledCurrent = c;
    }
    if (resistorPower == null && t.indexOf('resistor') === 0) {
      const p = lookupCI(powers, r.id);
      if (p != null) resistorPower = p;
    }
  }
  if (ledCurrent == null) {
    // Fall back to the LED finding's own message text.
    for (const f of findings) {
      const msg = String((f && f.message) || '');
      const m = /current ([\d.]+)\s*mA/.exec(msg);
      if (m && /LED/i.test(msg)) { ledCurrent = Number(m[1]) / 1000; break; }
    }
  }
  let rail = voltages.vcc != null ? voltages.vcc
    : (voltages['0'] != null ? voltages['0'] : null);
  if (rail == null) {
    const keys = Object.keys(voltages);
    rail = keys.length ? voltages[keys[0]] : null;
  }
  return { ledCurrent, resistorPower, rail };
}

// ---------------------------------------------------------------------------
// Styles (injected once; blueprint blue #0B3D91 only, focus #FFD166)
// NOTE 2026-11-15: these rules are staged for the move into
// web/styles.css in /tmp/blueprint-3d-css.md (styles.css is being
// edited concurrently). Do NOT delete this injector until those rules
// have landed there — see that file's cascade warnings first.
function injectCalloutStyles() {
  if (styleInjected || typeof document === 'undefined') return;
  const style = document.createElement('style');
  style.textContent =
    '.netproof-callout-svg{position:absolute;left:0;top:0;width:100%;height:100%;' +
    'pointer-events:none;z-index:6;overflow:hidden;}' +
    '.netproof-callout-layer{position:absolute;left:0;top:0;right:0;bottom:0;' +
    'overflow:hidden;pointer-events:none;z-index:7;}' +
    '.netproof-callout{display:block;box-sizing:border-box;width:260px;min-width:44px;' +
    'min-height:44px;padding:10px 12px;border:1px solid #0B3D91;border-radius:8px;' +
    'background:#ffffff;color:#1c1a17;text-align:left;cursor:pointer;' +
    'font:13px/1.45 "IBM Plex Sans",system-ui,sans-serif;pointer-events:auto;}' +
    '.netproof-callout .nc-head{display:flex;justify-content:space-between;gap:8px;}' +
    '.netproof-callout .nc-rule{font:600 12px/1.4 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;}' +
    '.netproof-callout .nc-sev{font:600 12px/1.4 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;white-space:nowrap;}' +
    '.netproof-callout .nc-comp{font:12px/1.4 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;color:#595959;white-space:nowrap;}' +
    '.netproof-callout .nc-msg{display:block;margin-top:4px;}' +
    '.netproof-callout .nc-measured{display:block;margin-top:4px;' +
    'font:12px/1.4 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;color:#595959;}' +
    '.netproof-callout .nc-measured b{color:#1c1a17;font-weight:600;}' +
    '.netproof-callout .nc-rechecking{display:block;margin-top:4px;' +
    'font:12px/1.4 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;color:#595959;}' +
    '.netproof-callout-verdict{position:absolute;top:12px;left:12px;width:300px;box-sizing:border-box;' +
    'padding:14px 16px;border:1px solid #1a7f37;border-radius:8px;background:#ffffff;color:#1c1a17;' +
    'pointer-events:none;box-shadow:0 2px 10px rgba(0,0,0,0.12);}' +
    '.netproof-callout-verdict .nc-eyebrow{font:600 11px/1.4 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;' +
    'letter-spacing:0.12em;color:#595959;}' +
    '.netproof-callout-verdict .nc-word{font:700 44px/1.1 "IBM Plex Sans",system-ui,sans-serif;color:#1c1a17;}' +
    '.netproof-callout-verdict .nc-mock{display:inline-block;margin-left:8px;padding:2px 6px;border-radius:4px;' +
    'background:#111;color:#ffe600;font:700 11px/1.4 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;' +
    'vertical-align:middle;}' +
    '.netproof-callout-verdict .nc-readouts{margin-top:8px;}' +
    '.netproof-callout-verdict .nc-readout{display:flex;justify-content:space-between;gap:8px;' +
    'font:13px/1.6 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;}' +
    '.netproof-callout-verdict .nc-readout .nc-label{color:#595959;}' +
    '.netproof-callout-verdict .nc-readout .nc-value{font-weight:600;color:#1c1a17;}' +
    '.netproof-callout-detail{cursor:default;}' +
    '.netproof-callout-detail .nc-row{display:flex;justify-content:space-between;gap:8px;' +
    'font:12px/1.7 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;}' +
    '.netproof-callout-detail .nc-row .nc-label{color:#595959;}' +
    '.netproof-callout-detail .nc-row .nc-value{font-weight:600;color:#1c1a17;text-align:right;}' +
    '.netproof-callout-detail .nc-chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;}' +
    '.netproof-callout-detail .nc-chip{min-height:44px;min-width:44px;padding:10px 12px;' +
    'border:1px solid #0B3D91;border-radius:6px;background:#ffffff;cursor:pointer;' +
    'font:600 12px/1.2 "IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;}' +
    '.netproof-callout:focus-visible,.netproof-callout-detail:focus-visible,' +
    '.netproof-callout-detail .nc-chip:focus-visible{' +
    'outline:3px solid ' + FOCUS_RING + ';outline-offset:2px;' +
    'box-shadow:0 0 0 4px ' + FOCUS_KEYLINE + ';border-radius:4px;scroll-margin:72px;}' +
    '@media (prefers-reduced-motion:reduce){' +
    '.netproof-callout,.netproof-callout-verdict{transition:none !important;animation:none !important;}' +
    '.netproof-callout-detail .nc-value-tween{transition:none !important;}}' +
    '.netproof-callout-detail .nc-value-tween{transition:opacity 150ms ease,transform 150ms ease;}' +
    '.netproof-callout-detail .nc-value-tweening{opacity:0;transform:translateY(2px);}' +
    '[data-theme="dark"] .netproof-callout,[data-theme="dark"] .netproof-callout-verdict,' +
    '[data-theme="dark"] .netproof-callout-detail .nc-chip{background:#232320;color:#edeae2;}' +
    '[data-theme="dark"] .netproof-callout .nc-comp,' +
    '[data-theme="dark"] .netproof-callout .nc-measured,' +
    '[data-theme="dark"] .netproof-callout .nc-rechecking,' +
    '[data-theme="dark"] .netproof-callout-verdict .nc-eyebrow,' +
    '[data-theme="dark"] .netproof-callout-verdict .nc-readout .nc-label,' +
    '[data-theme="dark"] .netproof-callout-detail .nc-row .nc-label{color:#b3aea1;}' +
    '[data-theme="dark"] .netproof-callout .nc-measured b,' +
    '[data-theme="dark"] .netproof-callout-verdict .nc-word,' +
    '[data-theme="dark"] .netproof-callout-verdict .nc-readout .nc-value,' +
    '[data-theme="dark"] .netproof-callout-detail .nc-row .nc-value{color:#edeae2;}';
  document.head.appendChild(style);
  styleInjected = true;
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

export function initCallouts(options) {
  const opts = options || {};
  if (opts.THREE) THREE = opts.THREE;
  else if (typeof window !== 'undefined' && window.THREE) THREE = window.THREE;
  if (opts.scene) scene = opts.scene;
  if (opts.camera) camera = opts.camera;
  if (typeof opts.onSelect === 'function') onSelectCb = opts.onSelect;
  if (typeof opts.onActivate === 'function') onActivateCb = opts.onActivate;
  if (typeof document === 'undefined') return null;
  if (!opts.container && !container) return null;
  if (opts.container) container = opts.container;
  if (svg && layer) return calloutsHandle();
  injectCalloutStyles();
  svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('class', 'netproof-callout-svg');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  container.appendChild(svg);
  layer = document.createElement('div');
  layer.className = 'netproof-callout-layer';
  container.appendChild(layer);
  return calloutsHandle();
}

// The handle initCallouts() returns: every name main3d.js's adapter
// (and overlay-style consumers) probe for, bound to the real
// functions. layoutCallouts is the per-frame recompute; updatePositions,
// recompute and layout are aliases of it (see below).
function calloutsHandle() {
  return {
    element: layer,
    svg,
    layer,
    update: updateCallouts,
    updateCallouts,
    layoutCallouts,
    layout: layoutCallouts,
    updatePositions,
    recompute,
    dispose: disposeCallouts,
  };
}

function ensureInit() {
  if (!layer && typeof document !== 'undefined' && container) initCallouts({});
  return !!layer;
}

function dispatchSelect(componentId, source) {
  if (typeof onSelectCb === 'function') {
    try { onSelectCb(componentId, source); } catch (e) { /* page callback; never break layout */ }
  }
  if (container && typeof CustomEvent !== 'undefined') {
    container.dispatchEvent(new CustomEvent('netproof:callout-select', {
      bubbles: true,
      detail: { componentId: componentId || null, source: source || 'callout' },
    }));
  }
}

function dispatchActivate(componentId, kind) {
  if (typeof onActivateCb === 'function') {
    try { onActivateCb(componentId, kind); } catch (e) { /* page callback */ }
  }
  if (container && typeof CustomEvent !== 'undefined') {
    container.dispatchEvent(new CustomEvent('netproof:callout-activate', {
      bubbles: true,
      detail: { componentId: componentId || null, kind },
    }));
  }
}

// ---------------------------------------------------------------------------
// Component info (netlist first, mesh userData second — never invented)
// ---------------------------------------------------------------------------

function componentInfo(id) {
  const fromReport = componentsById[id];
  if (fromReport) return fromReport;
  const mesh = meshesById[id];
  const ud = mesh && mesh.userData ? mesh.userData : {};
  return {
    id,
    type: ud.type || '',
    value: ud.value != null ? ud.value : '',
    nodes: Array.isArray(ud.nodes) ? ud.nodes.slice()
      : (nodesById[id] ? nodesById[id].slice() : []),
    rating_watts: ud.ratingWatts != null ? ud.ratingWatts : undefined,
    color: ud.color || undefined,
  };
}

function allComponents() {
  const ids = new Set([
    ...Object.keys(componentsById),
    ...Object.keys(meshesById),
    ...Object.keys(nodesById),
  ]);
  return [...ids].map((id) => componentInfo(id));
}

// Measured values for one part, straight from the Report. overlay.js
// passes measuredById; when it is absent this computes the same thing
// locally (same lookups, same "—" for missing values).
function measuredFor(id) {
  if (measuredById[id]) return measuredById[id];
  if (!currentReport) return { voltages: [], current: undefined, power: undefined };
  const nodeVoltages = currentReport.node_voltages || currentReport.nodeVoltages || {};
  const branchCurrents = currentReport.branch_currents || currentReport.branchCurrents || {};
  const power = currentReport.power || currentReport.per_component_power
    || currentReport.component_power || {};
  const nodes = componentInfo(id).nodes || [];
  const voltages = [];
  for (const node of nodes) {
    voltages.push({ node, value: lookupCI(nodeVoltages, node) });
  }
  return {
    voltages,
    current: lookupCI(branchCurrents, id),
    power: lookupCI(power, id),
  };
}

// ---------------------------------------------------------------------------
// Verdict callout (top-left; the only live region on the canvas)
// ---------------------------------------------------------------------------

function verdictSignature(report) {
  if (!report) return '';
  const readings = keyReadings(report, allComponents());
  return JSON.stringify([
    report.verdict, !!report.mock,
    readings.ledCurrent, readings.resistorPower, readings.rail,
  ]);
}

function buildVerdictCallout(report) {
  if (!ensureInit()) return;
  if (!report || !report.verdict) {
    // Before any check there is no verdict box on the canvas.
    if (verdictEl) verdictEl.style.display = 'none';
    lastVerdictSignature = '';
    return;
  }
  const sig = verdictSignature(report);
  const verdict = String(report.verdict).toUpperCase();
  const border = CALLOUT_SEVERITY_COLORS[verdict] || CALLOUT_SEVERITY_COLORS.UNKNOWN;
  if (!verdictEl) {
    verdictEl = document.createElement('div');
    verdictEl.className = 'netproof-callout-verdict';
    verdictEl.setAttribute('role', 'status');
    const eyebrow = document.createElement('div');
    eyebrow.className = 'nc-eyebrow';
    eyebrow.textContent = 'VERDICT';
    verdictWordEl = document.createElement('div');
    verdictWordEl.className = 'nc-word';
    verdictReadoutsEl = document.createElement('div');
    verdictReadoutsEl.className = 'nc-readouts';
    verdictEl.appendChild(eyebrow);
    verdictEl.appendChild(verdictWordEl);
    verdictEl.appendChild(verdictReadoutsEl);
    layer.appendChild(verdictEl);
  }
  verdictEl.style.display = 'block';
  verdictEl.style.borderColor = border;
  verdictEl.setAttribute('aria-live',
    verdict === 'STOP' ? 'assertive' : 'polite');
  if (sig !== lastVerdictSignature) {
    const readings = keyReadings(report, allComponents());
    verdictWordEl.textContent = '';
    verdictWordEl.appendChild(document.createTextNode(verdict));
    if (report.mock) {
      const mock = document.createElement('span');
      mock.className = 'nc-mock';
      mock.textContent = 'MOCK';
      verdictWordEl.appendChild(mock);
    }
    const rows = [
      ['LED current', fmtCurrent(readings.ledCurrent)],
      ['Resistor power', fmtPower(readings.resistorPower)],
      ['Supply', fmtVolts(readings.rail)],
    ];
    verdictReadoutsEl.textContent = '';
    for (const [label, value] of rows) {
      const row = document.createElement('div');
      row.className = 'nc-readout';
      const lab = document.createElement('span');
      lab.className = 'nc-label';
      lab.textContent = label;
      const val = document.createElement('span');
      val.className = 'nc-value';
      val.textContent = value;
      row.appendChild(lab);
      row.appendChild(val);
      verdictReadoutsEl.appendChild(row);
    }
    lastVerdictSignature = sig;
  }
}

// ---------------------------------------------------------------------------
// Finding callouts (one <button> per finding, Report order)
// ---------------------------------------------------------------------------

function findingKey(finding, index) {
  return String(finding.rule_id || finding.code || finding.rule || ('finding-' + index))
    + '|' + index;
}

function findingAriaLabel(finding, sev, componentId, measured) {
  const parts = [sev + '.', String(finding.rule_id || finding.code || finding.rule || 'Finding') + '.'];
  parts.push(String(finding.message || ''));
  if (measured) {
    parts.push('Measured ' + measured.measured + '. Limit ' + measured.limit + '.');
  }
  if (componentId) parts.push('Part ' + componentId + '. Press Enter to show details for ' + componentId + '.');
  else parts.push('No part named by this finding.');
  return parts.join(' ');
}

function buildFindingCallouts(report) {
  if (!ensureInit()) return;
  const findings = Array.isArray(report.findings) ? report.findings : [];
  const sig = JSON.stringify(findings.map((f) => [
    f.rule_id || f.code || f.rule || '',
    f.severity || f.level || '',
    componentIdsOf(f),
    f.message || f.text || f.detail || '',
  ]));
  if (sig === lastFindingsSignature) return;
  lastFindingsSignature = sig;

  // Remove callouts that no longer exist.
  for (const [key, item] of findingItems) {
    if (item.el && typeof item.el.remove === 'function') item.el.remove();
    if (item.line && typeof item.line.remove === 'function') item.line.remove();
    if (item.dot && typeof item.dot.remove === 'function') item.dot.remove();
    findingItems.delete(key);
  }

  findings.forEach((finding, index) => {
    if (!finding || typeof finding !== 'object') return;
    const sev = severityOf(finding);
    const border = CALLOUT_SEVERITY_COLORS[sev];
    const textColor = SEVERITY_TEXT_COLORS[sev];
    const ids = componentIdsOf(finding);
    const componentId = ids[0] || null;
    const extra = ids.length > 1 ? ' +' + (ids.length - 1) : '';
    const message = String(finding.message || finding.text || finding.detail || '');
    const measured = parseMeasured(message);

    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'netproof-callout';
    el.dataset.ruleId = String(finding.rule_id || finding.code || finding.rule || '');
    if (componentId) el.dataset.componentId = componentId;
    el.style.borderColor = border;
    el.setAttribute('aria-label', findingAriaLabel(finding, sev, componentId, measured));

    const head = document.createElement('span');
    head.className = 'nc-head';
    const rule = document.createElement('span');
    rule.className = 'nc-rule';
    rule.textContent = String(finding.rule_id || finding.code || finding.rule || 'FINDING');
    const sevEl = document.createElement('span');
    sevEl.className = 'nc-sev';
    sevEl.textContent = sev;
    sevEl.style.color = textColor;
    head.appendChild(rule);
    head.appendChild(sevEl);
    if (componentId) {
      const comp = document.createElement('span');
      comp.className = 'nc-comp';
      comp.textContent = componentId + extra;
      head.appendChild(comp);
    }
    const msg = document.createElement('span');
    msg.className = 'nc-msg';
    msg.textContent = message;
    el.appendChild(head);
    el.appendChild(msg);
    if (measured) {
      const ml = document.createElement('span');
      ml.className = 'nc-measured';
      ml.appendChild(document.createTextNode('Measured '));
      const mv = document.createElement('b');
      mv.textContent = measured.measured;
      ml.appendChild(mv);
      ml.appendChild(document.createTextNode(' · Limit '));
      const lv = document.createElement('b');
      lv.textContent = measured.limit;
      ml.appendChild(lv);
      el.appendChild(ml);
    }

    el.addEventListener('click', () => {
      if (componentId) {
        openDetailCallout(componentId);
        dispatchSelect(componentId, 'finding-callout');
      }
    });
    el.addEventListener('keydown', (event) => {
      const keys = [...findingItems.values()].map((it) => it.el);
      const at = keys.indexOf(el);
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
        event.preventDefault();
        const next = keys[(at + 1) % keys.length];
        if (next) next.focus();
      } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        event.preventDefault();
        const prev = keys[(at - 1 + keys.length) % keys.length];
        if (prev) prev.focus();
      } else if (event.key === 'Home') {
        event.preventDefault();
        if (keys[0]) keys[0].focus();
      } else if (event.key === 'End') {
        event.preventDefault();
        if (keys.length) keys[keys.length - 1].focus();
      }
      // Enter / Space fire click natively: select + detail callout.
    });

    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('stroke', border);
    line.setAttribute('stroke-width', '1');
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('r', String(DOT_RADIUS));
    dot.setAttribute('fill', border);
    dot.setAttribute('stroke', '#ffffff');
    dot.setAttribute('stroke-width', '1');
    svg.appendChild(line);
    svg.appendChild(dot);
    layer.appendChild(el);
    findingItems.set(findingKey(finding, index), {
      el, line, dot, finding, componentId, sev, index,
    });
  });
}

// ---------------------------------------------------------------------------
// Detail callout (click a part; one open at a time)
// ---------------------------------------------------------------------------

function detailAriaLabel(id, info, measured, acrossText, partFindings) {
  const bits = [id + ', ' + (info.type || 'component') + '.'];
  const value = fmtValueForType(info.type, info.value);
  if (value) bits.push('Value ' + value + '.');
  for (const v of measured.voltages) {
    bits.push('Node ' + v.node + ' ' + fmtVolts(v.value) + '.');
  }
  bits.push('Voltage across ' + acrossText + '.');
  bits.push('Current ' + fmtCurrent(measured.current) + '.');
  bits.push('Power ' + fmtPower(measured.power) + '.');
  if (partFindings.length) {
    bits.push('Findings: ' + partFindings.map((f) => severityOf(f) + ' '
      + (f.rule_id || f.code || f.rule || '')).join(', ') + '.');
  }
  bits.push('Press Enter to edit, Escape to close.');
  return bits.join(' ');
}

export function openDetailCallout(componentId) {
  if (!ensureInit()) return false;
  const id = String(componentId);
  if (!meshesById[id] && !componentsById[id] && !nodesById[id]) return false;
  if (detail && detail.id === id) return true;
  closeDetailCallout();
  const info = componentInfo(id);
  const measured = measuredFor(id);
  const findings = currentReport ? (currentReport.findings || []) : [];
  const partFindings = findingsForComponent(id, findings);
  const sev = severityForComponentLocal(id, findings,
    currentReport ? currentReport.verdict : undefined);
  const border = CALLOUT_SEVERITY_COLORS[sev] || CALLOUT_SEVERITY_COLORS.UNKNOWN;

  const el = document.createElement('div');
  el.className = 'netproof-callout netproof-callout-detail';
  el.tabIndex = 0;
  el.setAttribute('role', 'group');
  el.dataset.componentId = id;
  el.style.borderColor = border;

  const head = document.createElement('div');
  head.className = 'nc-head';
  const rule = document.createElement('span');
  rule.className = 'nc-rule';
  rule.textContent = id;
  const comp = document.createElement('span');
  comp.className = 'nc-comp';
  comp.textContent = info.type || '';
  head.appendChild(rule);
  head.appendChild(comp);
  el.appendChild(head);

  const rows = [];
  const valueText = fmtValueForType(info.type, info.value);
  rows.push(['Value', valueText || '—', 'value']);
  for (const v of measured.voltages) {
    rows.push(['V(' + v.node + ')', fmtVolts(v.value), null]);
  }
  // Voltage across: V(n1) - V(n2) from Report node voltages, labelled
  // with the actual node names (edit-panel.js wording).
  let acrossText = '—';
  const nodes = info.nodes || [];
  if (nodes.length >= 2) {
    const v1 = lookupCI(
      currentReport ? (currentReport.node_voltages || currentReport.nodeVoltages || {}) : {},
      nodes[0]);
    const v2 = lookupCI(
      currentReport ? (currentReport.node_voltages || currentReport.nodeVoltages || {}) : {},
      nodes[1]);
    if (isNum(v1) && isNum(v2)) acrossText = fmtVolts(v1 - v2);
    rows.push(['V(' + nodes[0] + ') − V(' + nodes[1] + ')', acrossText, null]);
  }
  rows.push(['Current', fmtCurrent(measured.current), null]);
  rows.push(['Power', fmtPower(measured.power), null]);
  for (const [label, value, kind] of rows) {
    const row = document.createElement('div');
    row.className = 'nc-row';
    const lab = document.createElement('span');
    lab.className = 'nc-label';
    lab.textContent = label;
    const val = document.createElement('span');
    val.className = 'nc-value';
    if (kind === 'value') val.dataset.detailValue = 'true';
    val.textContent = value;
    row.appendChild(lab);
    row.appendChild(val);
    el.appendChild(row);
  }

  const chips = new Map();
  if (partFindings.length) {
    const wrap = document.createElement('div');
    wrap.className = 'nc-chips';
    for (const f of partFindings) {
      const fsev = severityOf(f);
      const ruleId = String(f.rule_id || f.code || f.rule || '');
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'nc-chip';
      chip.textContent = ruleId;
      chip.style.borderColor = CALLOUT_SEVERITY_COLORS[fsev];
      chip.style.color = SEVERITY_TEXT_COLORS[fsev];
      chip.setAttribute('aria-label', 'Finding ' + fsev + ' ' + ruleId
        + ' for ' + id + '. Activate to focus its callout.');
      chip.addEventListener('click', (event) => {
        event.stopPropagation();
        for (const item of findingItems.values()) {
          if (String(item.finding.rule_id || item.finding.code || item.finding.rule || '') === ruleId
            && item.componentId === id) {
            item.el.focus();
            return;
          }
        }
        for (const item of findingItems.values()) {
          if (String(item.finding.rule_id || item.finding.code || item.finding.rule || '') === ruleId) {
            item.el.focus();
            return;
          }
        }
      });
      wrap.appendChild(chip);
      chips.set(ruleId, chip);
    }
    el.appendChild(wrap);
  }

  el.setAttribute('aria-label', detailAriaLabel(id, info, measured, acrossText, partFindings));
  el.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      // Close the detail callout first; the event keeps bubbling so
      // interaction.js deselects on the same press, after the close.
      closeDetailCallout();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      dispatchActivate(id, 'detail');
    }
  });

  const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
  line.setAttribute('stroke', border);
  line.setAttribute('stroke-width', '1');
  const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
  dot.setAttribute('r', String(DOT_RADIUS));
  dot.setAttribute('fill', border);
  dot.setAttribute('stroke', '#ffffff');
  dot.setAttribute('stroke-width', '1');
  svg.appendChild(line);
  svg.appendChild(dot);
  layer.appendChild(el);
  detail = { id, el, line, dot, chips, sev };
  return true;
}

export function closeDetailCallout() {
  if (!detail) return false;
  if (detail.el && typeof detail.el.remove === 'function') detail.el.remove();
  if (detail.line && typeof detail.line.remove === 'function') detail.line.remove();
  if (detail.dot && typeof detail.dot.remove === 'function') detail.dot.remove();
  detail = null;
  return true;
}

export function getDetailComponentId() {
  return detail ? detail.id : null;
}

// ---------------------------------------------------------------------------
// Repair diff callouts (strings come from the page's diffNetlists)
// ---------------------------------------------------------------------------

export function showRepairDiffs(diffs, options) {
  if (!ensureInit()) return 0;
  const opts = options || {};
  closeRepairDiffs();
  diffRechecking = opts.rechecking === true;
  const list = Array.isArray(diffs) ? diffs : [];
  const byId = new Map();
  for (const d of list) {
    let id = null;
    let text = '';
    if (typeof d === 'string') {
      text = d;
      id = d.split(/\s+/)[0] || null;
    } else if (d && typeof d === 'object') {
      id = d.id ? String(d.id) : (d.componentId ? String(d.componentId) : null);
      text = String(d.text || d.message || d.description || '');
      if (!text && d.lines) text = d.lines.join('\n');
    }
    if (!id || !text) continue;
    if (!byId.has(id)) byId.set(id, []);
    byId.get(id).push(text);
  }
  const findings = currentReport ? (currentReport.findings || []) : [];
  const verdict = currentReport ? currentReport.verdict : undefined;
  for (const [id, lines] of byId) {
    const sev = severityForComponentLocal(id, findings, verdict);
    const border = CALLOUT_SEVERITY_COLORS[sev] || CALLOUT_SEVERITY_COLORS.UNKNOWN;
    const el = document.createElement('button');
    el.type = 'button';
    el.className = 'netproof-callout';
    el.dataset.componentId = id;
    el.style.borderColor = border;
    el.setAttribute('aria-label', 'Repair change for ' + id + '. '
      + lines.join('. ') + '. Press Enter to show details for ' + id + '.');
    const head = document.createElement('span');
    head.className = 'nc-head';
    const rule = document.createElement('span');
    rule.className = 'nc-rule';
    rule.textContent = 'REPAIR';
    const sevEl = document.createElement('span');
    sevEl.className = 'nc-sev';
    sevEl.textContent = sev;
    sevEl.style.color = SEVERITY_TEXT_COLORS[sev];
    head.appendChild(rule);
    head.appendChild(sevEl);
    el.appendChild(head);
    for (const text of lines) {
      const msg = document.createElement('span');
      msg.className = 'nc-msg';
      msg.textContent = text;
      el.appendChild(msg);
    }
    if (diffRechecking) {
      const rc = document.createElement('span');
      rc.className = 'nc-rechecking';
      rc.textContent = 're-checking…';
      el.appendChild(rc);
    }
    el.addEventListener('click', () => {
      openDetailCallout(id);
      dispatchSelect(id, 'repair-diff');
    });
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('stroke', border);
    line.setAttribute('stroke-width', '1');
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('r', String(DOT_RADIUS));
    dot.setAttribute('fill', border);
    dot.setAttribute('stroke', '#ffffff');
    dot.setAttribute('stroke-width', '1');
    svg.appendChild(line);
    svg.appendChild(dot);
    layer.appendChild(el);
    diffItems.set(id, { el, line, dot, lines, componentId: id, sev });

    // Repair value tween: if the detail callout is open for this part,
    // its value switches at the midpoint of a ~300 ms fade/slide.
    // prefers-reduced-motion: instant switch, no tween.
    if (detail && detail.id === id) {
      const valueEl = detail.el.querySelector('[data-detail-value]');
      if (valueEl) {
        const info = componentInfo(id);
        const next = fmtValueForType(info.type, info.value) || '—';
        const reduced = typeof window !== 'undefined' && window.matchMedia
          && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        if (reduced || next === valueEl.textContent) {
          valueEl.textContent = next;
        } else {
          valueEl.classList.add('nc-value-tween', 'nc-value-tweening');
          setTimeout(() => {
            valueEl.textContent = next;
            valueEl.classList.remove('nc-value-tweening');
            setTimeout(() => valueEl.classList.remove('nc-value-tween'), 160);
          }, 150);
        }
      }
    }
  }
  return diffItems.size;
}

export function closeRepairDiffs() {
  let n = 0;
  for (const [, item] of diffItems) {
    if (item.el && typeof item.el.remove === 'function') item.el.remove();
    if (item.line && typeof item.line.remove === 'function') item.line.remove();
    if (item.dot && typeof item.dot.remove === 'function') item.dot.remove();
    n++;
  }
  diffItems.clear();
  diffRechecking = false;
  return n;
}

export function setRepairRechecking(rechecking) {
  diffRechecking = rechecking === true;
  for (const [, item] of diffItems) {
    const rc = item.el.querySelector('.nc-rechecking');
    if (diffRechecking && !rc) {
      const el = document.createElement('span');
      el.className = 'nc-rechecking';
      el.textContent = 're-checking…';
      item.el.appendChild(el);
    } else if (!diffRechecking && rc) {
      rc.remove();
    }
  }
}

// ---------------------------------------------------------------------------
// Update (rebuild content on a new Report; layout runs every frame)
// ---------------------------------------------------------------------------

export function updateCallouts(report, nextMeshesById, extra) {
  if (typeof document === 'undefined') return;
  // No Report, no callouts: verdict and findings both come from the
  // Report (buildVerdictCallout hides the verdict box before any
  // check), so there is nothing to render yet. Per-part id/type/value
  // still show without a Report via the click-to-open detail callout,
  // which reads the mesh userData, not the Report.
  if (!report || typeof report !== 'object') return;
  if (!ensureInit()) return;
  currentReport = report;
  if (nextMeshesById) meshesById = nextMeshesById;
  const opts = extra || {};
  if (opts.nodesById) nodesById = opts.nodesById;
  if (opts.measuredById) measuredById = opts.measuredById;
  componentsById = {};
  for (const c of componentsFromReport(report)) componentsById[c.id] = c;
  if (Array.isArray(opts.components)) {
    for (const c of opts.components) {
      const row = normRow(c);
      if (row.id && !componentsById[row.id]) componentsById[row.id] = row;
    }
  }
  // Mesh userData carries type/value/nodes for parts the Report's
  // netlist_used does not name (e.g. a report from a history round).
  for (const id of Object.keys(meshesById)) {
    if (!componentsById[id]) {
      const info = componentInfo(id);
      if (info.type || info.value !== '' || (info.nodes && info.nodes.length)) {
        componentsById[id] = info;
      }
    }
  }
  buildVerdictCallout(report);
  buildFindingCallouts(report);
  // A new Report re-renders an open detail callout from the new
  // numbers (same part, same position); diff callouts stay pinned
  // until the user clicks elsewhere or a new repair replaces them.
  if (detail) {
    const id = detail.id;
    closeDetailCallout();
    openDetailCallout(id);
  }
  layoutCallouts(camera);
}

// ---------------------------------------------------------------------------
// Projection + edge-lane layout (recomputed from scratch every frame)
// ---------------------------------------------------------------------------

function computeAnchor(mesh) {
  if (!THREE || !mesh) return null;
  try {
    if (typeof THREE.Box3 === 'function') {
      const box = new THREE.Box3().setFromObject(mesh);
      const center = box.getCenter(new THREE.Vector3());
      return new THREE.Vector3(center.x, box.max.y + 0.15, center.z);
    }
    if (typeof mesh.getWorldPosition === 'function') {
      return mesh.getWorldPosition(new THREE.Vector3());
    }
  } catch (e) {
    return null;
  }
  return null;
}

function projectAnchor(mesh, activeCam, width, height) {
  if (!THREE || !activeCam) return null;
  const anchor = computeAnchor(mesh);
  if (!anchor) return null;
  const v = anchor.clone().project(activeCam);
  if (v.z > 1 || v.z < -1) return { behind: true };
  return {
    x: (v.x * 0.5 + 0.5) * width,
    y: (-v.y * 0.5 + 0.5) * height,
    behind: false,
  };
}

function rectsOverlap(a, b, gap) {
  const g = gap || 0;
  return a.left < b.right + g && a.right > b.left - g
    && a.top < b.bottom + g && a.bottom > b.top - g;
}

function clamp(v, lo, hi) {
  return Math.min(Math.max(v, lo), Math.max(lo, hi));
}

// Closest point on a box edge to a point outside it (the leader's
// box end). When the point is inside the box, the nearest edge wins.
function boxEdgePoint(rect, px, py) {
  const cx = clamp(px, rect.left, rect.right);
  const cy = clamp(py, rect.top, rect.bottom);
  if (cx !== px || cy !== py) return { x: cx, y: cy };
  const dLeft = px - rect.left;
  const dRight = rect.right - px;
  const dTop = py - rect.top;
  const dBottom = rect.bottom - py;
  const m = Math.min(dLeft, dRight, dTop, dBottom);
  if (m === dLeft) return { x: rect.left, y: py };
  if (m === dRight) return { x: rect.right, y: py };
  if (m === dTop) return { x: px, y: rect.top };
  return { x: px, y: rect.bottom };
}

export function layoutCallouts(cam) {
  const activeCam = cam || camera;
  if (!svg || !layer || !container) return;
  const W = container.clientWidth;
  const H = container.clientHeight;
  if (!W || !H) return;

  // Box width: 260 px on a wide canvas, narrower on a small one so
  // two lanes still fit; never below 160 px.
  const boxW = W >= 600 ? CALLOUT_WIDTH : Math.max(160, Math.floor(W * 0.46));
  const verdictW = Math.min(VERDICT_WIDTH, Math.max(200, W - 24));
  if (verdictEl && verdictEl.style.display !== 'none') {
    verdictEl.style.width = verdictW + 'px';
  }

  // Project every part anchor once; build the circuit screen box.
  const anchorById = new Map();
  const anchorPoints = [];
  for (const id of Object.keys(meshesById)) {
    const p = projectAnchor(meshesById[id], activeCam, W, H);
    if (!p) continue;
    anchorById.set(id, p);
    if (!p.behind) anchorPoints.push(p);
  }
  let circuitBox = null;
  if (anchorPoints.length) {
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    for (const p of anchorPoints) {
      if (p.x < left) left = p.x;
      if (p.y < top) top = p.y;
      if (p.x > right) right = p.x;
      if (p.y > bottom) bottom = p.y;
    }
    circuitBox = {
      left: left - CIRCUIT_PAD,
      top: top - CIRCUIT_PAD,
      right: right + CIRCUIT_PAD,
      bottom: bottom + CIRCUIT_PAD,
    };
  }

  const verdictVisible = verdictEl && verdictEl.style.display !== 'none';
  const verdictRect = verdictVisible ? {
    left: VERDICT_LEFT,
    top: VERDICT_TOP,
    right: VERDICT_LEFT + verdictEl.offsetWidth,
    bottom: VERDICT_TOP + verdictEl.offsetHeight,
  } : null;
  const laneTop = {
    left: verdictRect ? verdictRect.bottom + 12 : LANE_MARGIN,
    right: LANE_MARGIN,
  };

  // Collect placeable items. Rank decides placement order inside a
  // lane: globals first (under the verdict), then the detail callout
  // (lane priority: findings shift down for it), then findings, then
  // diffs. Within a rank, sort by projected anchor y.
  const items = [];
  const pushItem = (kind, el, line, dot, componentId, index) => {
    const anchor = componentId ? anchorById.get(componentId) : null;
    if (anchor && anchor.behind) {
      el.style.display = 'none';
      if (line) line.style.display = 'none';
      if (dot) dot.style.display = 'none';
      return;
    }
    el.style.display = 'block';
    el.style.width = boxW + 'px';
    items.push({
      kind, el, line, dot, componentId, index,
      anchor: anchor && !anchor.behind ? anchor : null,
      flipped: false, rect: null, overflow: false,
    });
  };
  for (const item of findingItems.values()) {
    pushItem('finding', item.el, item.line,
      item.dot, item.componentId, item.index);
  }
  if (detail) {
    pushItem('detail', detail.el, detail.line, detail.dot,
      detail.id, -1);
  }
  for (const item of diffItems.values()) {
    pushItem('diff', item.el, item.line, item.dot,
      item.componentId, 1000);
  }

  const lanes = { left: [], right: [] };
  for (const item of items) {
    const side = item.anchor && item.anchor.x >= W / 2 ? 'right' : 'left';
    lanes[side].push(item);
  }
  const rankOf = (kind) => ({ global: 0, detail: 1, finding: 2, diff: 3 }[kind] ?? 2);
  for (const side of ['left', 'right']) {
    lanes[side].sort((a, b) => {
      const ra = a.componentId ? rankOf(a.kind) : 0;
      const rb = b.componentId ? rankOf(b.kind) : 0;
      if (ra !== rb) return ra - rb;
      const ay = a.anchor ? a.anchor.y : Infinity;
      const by = b.anchor ? b.anchor.y : Infinity;
      if (ay !== by) return ay - by;
      return a.index - b.index;
    });
  }

  // Place one lane. Returns placed items, items flipped to the other
  // lane (their leader would cross the circuit), and items that ran
  // out of vertical room (spill candidates).
  function placeLane(list, side, seedPlaced) {
    const placed = seedPlaced.slice();
    const flipped = [];
    const overflow = [];
    let cursor = laneTop[side];
    for (const item of list) {
      const w = item.el.offsetWidth || boxW;
      const h = item.el.offsetHeight || 60;
      const left = side === 'left' ? LANE_MARGIN : W - LANE_MARGIN - w;
      let top = item.anchor
        ? clamp(item.anchor.y - h / 2, laneTop[side], Math.max(laneTop[side], H - h - LANE_MARGIN))
        : cursor;
      let done = false;
      for (let attempt = 0; attempt < 30 && !done; attempt++) {
        const rect = { left, top, right: left + w, bottom: top + h };
        const hitPlaced = placed.find((p) => rectsOverlap(rect, p, LANE_GAP));
        if (hitPlaced) {
          top = hitPlaced.bottom + LANE_GAP;
          continue;
        }
        if (circuitBox && rectsOverlap(rect, circuitBox, 0)) {
          if (!item.flipped) {
            item.flipped = true;
            flipped.push(item);
            top = -1;
            done = true;
            continue;
          }
          // Flipped once already: dock just outside the circuit, on
          // the side nearer the anchor, then re-check placed rects.
          const cy = (circuitBox.top + circuitBox.bottom) / 2;
          top = (item.anchor && item.anchor.y > cy)
            ? circuitBox.bottom + LANE_GAP
            : circuitBox.top - h - LANE_GAP;
          top = clamp(top, laneTop[side], Math.max(laneTop[side], H - h - LANE_MARGIN));
          continue;
        }
        done = true;
      }
      if (top < 0) continue; // flipped to the other lane
      if (top + h > H - LANE_MARGIN) {
        item.overflow = true;
        overflow.push(item);
        continue;
      }
      const rect = { left, top, right: left + w, bottom: top + h };
      placed.push(rect);
      item.rect = rect;
      cursor = Math.max(cursor, rect.bottom + LANE_GAP);
    }
    return { placed, flipped, overflow };
  }

  const seedLeft = verdictRect ? [verdictRect] : [];
  let leftRes = placeLane(lanes.left, 'left', seedLeft);
  let rightRes = placeLane(lanes.right, 'right', []);

  // Flipped items go to the opposite lane and are placed after the
  // items already there (their leaders still point at their part).
  // placeLane returns placed including the seed rects it was given,
  // so the merged placed list is the returned one.
  const placeMoved = (moved, side, existingPlaced) =>
    placeLane(moved, side, existingPlaced);
  if (leftRes.flipped.length) {
    const res = placeMoved(leftRes.flipped, 'right', rightRes.placed);
    rightRes.placed = res.placed;
    rightRes.flipped = rightRes.flipped.concat(res.flipped);
    rightRes.overflow = rightRes.overflow.concat(res.overflow);
  }
  if (rightRes.flipped.length) {
    const res = placeMoved(rightRes.flipped, 'left', leftRes.placed);
    leftRes.placed = res.placed;
    leftRes.flipped = leftRes.flipped.concat(res.flipped);
    leftRes.overflow = leftRes.overflow.concat(res.overflow);
  }

  // Spill: a lane that ran out of room sends its lowest-priority
  // callouts (INFO first, then WARN) to the opposite lane's bottom.
  // STOP callouts and the detail callout never spill off-canvas.
  const spillPriority = (item) => {
    if (item.kind === 'detail') return 99;
    if (item.kind === 'diff') return 3;
    const sev = item.finding ? severityOf(item.finding) : 'INFO';
    return sev === 'INFO' ? 0 : sev === 'WARN' ? 1 : 2;
  };
  const spill = (fromSide, fromRes, toSide, toPlaced) => {
    if (!fromRes.overflow.length) return toPlaced;
    const cands = fromRes.overflow
      .filter((it) => spillPriority(it) < 2)
      .sort((a, b) => spillPriority(a) - spillPriority(b));
    if (!cands.length) return toPlaced;
    const res = placeLane(cands, toSide, toPlaced);
    return res.placed;
  };
  rightRes.placed = spill('left', leftRes, 'right', rightRes.placed);
  leftRes.placed = spill('right', rightRes, 'left', leftRes.placed);

  // Last resort for items still without a rect (both lanes full):
  // clamp them to the bottom of their lane rather than overlapping
  // another box. (Realistic fixture sizes never reach this.)
  for (const item of items) {
    if (item.rect) continue;
    const side = lanes.left.includes(item) ? 'left' : 'right';
    const w = item.el.offsetWidth || boxW;
    const h = item.el.offsetHeight || 60;
    const left = side === 'left' ? LANE_MARGIN : W - LANE_MARGIN - w;
    const top = Math.max(laneTop[side], H - h - LANE_MARGIN);
    item.rect = { left, top, right: left + w, bottom: top + h };
  }

  // Commit positions and redraw leaders.
  for (const item of items) {
    if (!item.rect) continue;
    item.el.style.left = item.rect.left + 'px';
    item.el.style.top = item.rect.top + 'px';
    item.el.style.transform = '';
    if (!item.line || !item.dot) continue;
    if (!item.anchor) {
      item.line.style.display = 'none';
      item.dot.style.display = 'none';
      continue;
    }
    const edge = boxEdgePoint(item.rect, item.anchor.x, item.anchor.y);
    item.line.style.display = '';
    item.dot.style.display = '';
    item.line.setAttribute('x1', edge.x.toFixed(1));
    item.line.setAttribute('y1', edge.y.toFixed(1));
    item.line.setAttribute('x2', item.anchor.x.toFixed(1));
    item.line.setAttribute('y2', item.anchor.y.toFixed(1));
    item.dot.setAttribute('cx', item.anchor.x.toFixed(1));
    item.dot.setAttribute('cy', item.anchor.y.toFixed(1));
  }
}

// ---------------------------------------------------------------------------
// Per-frame recompute aliases
// ---------------------------------------------------------------------------
// main3d.js's adapter (and overlay-style consumers) probe for
// updatePositions / recompute / layout. There is no separate
// incremental step: layout is recomputed from scratch every frame,
// so all three are layoutCallouts. The no-arg call uses the camera
// captured by initCallouts().
export function updatePositions(cam) {
  return layoutCallouts(cam);
}

export function recompute(cam) {
  return layoutCallouts(cam);
}

export function layout(cam) {
  return layoutCallouts(cam);
}

// ---------------------------------------------------------------------------
// Teardown
// ---------------------------------------------------------------------------

export function disposeCallouts() {
  closeDetailCallout();
  closeRepairDiffs();
  for (const [, item] of findingItems) {
    if (item.el && typeof item.el.remove === 'function') item.el.remove();
    if (item.line && typeof item.line.remove === 'function') item.line.remove();
    if (item.dot && typeof item.dot.remove === 'function') item.dot.remove();
  }
  findingItems.clear();
  lastFindingsSignature = '';
  lastVerdictSignature = '';
  if (verdictEl && typeof verdictEl.remove === 'function') verdictEl.remove();
  verdictEl = null;
  verdictReadoutsEl = null;
  verdictWordEl = null;
  if (svg && typeof svg.remove === 'function') svg.remove();
  if (layer && typeof layer.remove === 'function') layer.remove();
  svg = null;
  layer = null;
  container = null;
  currentReport = null;
  meshesById = {};
  nodesById = {};
  componentsById = {};
  measuredById = {};
  onSelectCb = null;
  onActivateCb = null;
}
